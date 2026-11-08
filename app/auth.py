# -*- coding: utf-8 -*-
"""登录鉴权 + 多账号权限。

- 账号存 data.db 的 users 表 (pbkdf2-hmac-sha256 哈希); 首次部署自动创建
  admin 账号 (初始密码取 config.ini 或随机生成并打印)
- 全部配置来自项目根 config.ini (经 app_config.load() 加载与校验)
- sid cookie = "username:exp:HMAC(secret, username:exp)", 默认 30 天
- 权限位: users/games/files/system/downloads; admin 固定全有且不可删除
- 路由用法: need_perm(request, "games") 单点完成 登录+鉴权 (401/403)
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data.db")
CONFIG_PATH = os.path.join(os.path.dirname(BASE), "config.ini")   # INI 配置 (app_config 管理)

PERMS = ["users", "games", "files", "system", "downloads"]
PERM_NAMES = {"users": "人员管理", "games": "游戏管理", "files": "全盘资源",
              "system": "系统高级", "downloads": "下载管理"}
ADMIN = "admin"

import app_config
CFG = app_config.load()                 # 统一配置: 全部来自项目根 config.ini
TOKEN_DAYS = int(CFG.get("token_days", 30))
PBKDF2_ITER = int(CFG.get("pbkdf2_iters", 120000))

SECRET = CFG["secret_key"].encode()

# 登录失败限速: 同一来源连续失败 5 次后 5 分钟内拒绝
_fail = {}


def _conn():
    return sqlite3.connect(DB_PATH, timeout=10)


def _hash_pwd(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(),
                               bytes.fromhex(salt_hex), PBKDF2_ITER).hex()


def ensure_users_table():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            pass_salt TEXT NOT NULL,
            pass_hash TEXT NOT NULL,
            perms    TEXT NOT NULL,
            created_ts REAL)""")


def migrate_admin():
    """首次部署: 创建 admin 账号 (初始密码取 config.ini 的 initial_admin_password,
    留空则生成随机密码并打印; 账号已存在则跳过)"""
    ensure_users_table()
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM users WHERE username=?", (ADMIN,)).fetchone()
    if exists:
        return
    pwd = CFG.get("initial_admin_password") or secrets.token_hex(6)
    salt = secrets.token_hex(16)
    with _conn() as c:
        c.execute("INSERT INTO users (username, pass_salt, pass_hash, perms, created_ts) "
                  "VALUES (?,?,?,?,?)",
                  (ADMIN, salt, _hash_pwd(pwd, salt), json.dumps(PERMS), time.time()))
    if not CFG.get("initial_admin_password"):
        print(f"[auth] 已创建管理员账号 {ADMIN}, 初始密码: {pwd} (请尽快登录修改)", flush=True)


# ---------------------------------------------------------------- 账号 CRUD (供 /api/users 用)
def get_user(username):
    with _conn() as c:
        row = c.execute("SELECT perms, created_ts FROM users WHERE username=?",
                        (username,)).fetchone()
    if not row:
        return None
    try:
        perms = [p for p in json.loads(row[0]) if p in PERMS]
    except Exception:
        perms = []
    return {"username": username, "perms": perms, "created_ts": row[1],
            "is_admin": username == ADMIN}


def list_users():
    ensure_users_table()
    with _conn() as c:
        rows = c.execute("SELECT username, perms, created_ts FROM users "
                         "ORDER BY created_ts").fetchall()
    out = []
    for name, perms, ts in rows:
        try:
            perms = [p for p in json.loads(perms) if p in PERMS]
        except Exception:
            perms = []
        out.append({"username": name, "perms": perms, "created_ts": ts,
                    "is_admin": name == ADMIN})
    return out


def create_user(username, password, perms):
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    if ":" in username or len(username) > 32 or username != username.strip():
        raise ValueError("用户名不能包含冒号或首尾空格, 最长 32 字符")
    if any(p not in PERMS for p in perms):
        raise ValueError("存在未知权限位")
    salt = secrets.token_hex(16)
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("用户已存在")
        c.execute("INSERT INTO users (username, pass_salt, pass_hash, perms, created_ts) "
                  "VALUES (?,?,?,?,?)",
                  (username, salt, _hash_pwd(password, salt),
                   json.dumps([p for p in PERMS if p in perms]), time.time()))


def update_perms(username, perms):
    if username == ADMIN:
        raise ValueError("admin 权限固定, 不可修改")
    if not get_user(username):
        raise ValueError("用户不存在")
    if any(p not in PERMS for p in perms):
        raise ValueError("存在未知权限位")
    with _conn() as c:
        c.execute("UPDATE users SET perms=? WHERE username=?",
                  (json.dumps([p for p in PERMS if p in perms]), username))


def reset_password(username, password):
    if not password:
        raise ValueError("密码不能为空")
    if not get_user(username):
        raise ValueError("用户不存在")
    salt = secrets.token_hex(16)
    with _conn() as c:
        c.execute("UPDATE users SET pass_salt=?, pass_hash=? WHERE username=?",
                  (salt, _hash_pwd(password, salt), username))


def delete_user(username):
    if username == ADMIN:
        raise ValueError("admin 不可删除")
    with _conn() as c:
        cur = c.execute("DELETE FROM users WHERE username=?", (username,))
        if cur.rowcount != 1:
            raise ValueError("用户不存在")


def verify_login(username, password):
    ensure_users_table()
    with _conn() as c:
        row = c.execute("SELECT pass_salt, pass_hash FROM users WHERE username=?",
                        (username,)).fetchone()
    if not row:
        return False
    return hmac.compare_digest(_hash_pwd(password, row[0]), row[1])


# ---------------------------------------------------------------- token (sid cookie)
def make_token(username):
    exp = int(time.time()) + TOKEN_DAYS * 86400
    sig = hmac.new(SECRET, f"{username}:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{username}:{exp}:{sig}"


def check_token(token):
    """校验 sid cookie, 返回 username 或 None"""
    try:
        username, exp_s, sig = token.split(":", 2)
        exp = int(exp_s)
    except Exception:
        return None
    if exp < time.time():
        return None
    want = hmac.new(SECRET, f"{username}:{exp_s}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        return None
    return username


# ---------------------------------------------------------------- 请求级鉴权
def current_user(request: Request):
    username = check_token(request.cookies.get("sid") or "")
    if not username:
        return None
    return get_user(username)


def is_authed(request: Request) -> bool:
    return current_user(request) is not None


def need_perm(request: Request, *flags):
    """路由内单点鉴权: 未登录 401, 缺任一权限位 403, 返回当前用户"""
    u = current_user(request)
    if not u:
        raise HTTPException(401, "unauth")
    if not u["is_admin"] and not all(f in u["perms"] for f in flags):
        raise HTTPException(403, "forbidden")
    return u


# ---------------------------------------------------------------- 登录节流
def login_throttled(key: str) -> bool:
    rec = _fail.get(key)
    if not rec:
        return False
    count, until = rec
    return count >= 5 and time.time() < until


def record_fail(key: str):
    rec = _fail.get(key, [0, 0])
    rec[0] += 1
    if rec[0] >= 5:
        rec[1] = time.time() + 300
    _fail[key] = rec


def clear_fail(key: str):
    _fail.pop(key, None)
