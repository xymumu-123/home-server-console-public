# -*- coding: utf-8 -*-
"""AI 助手: DeepSeek Harness (dsh) 无头驱动 + SSE 流式 + 按账号会话隔离

架构: 网页聊天 → FastAPI 鉴权(会话+ai权限位) → 每条消息驱动一次
      `node dsh.js --profile headless --json [--session-id] "<消息>"` →
      解析按行 JSON 事件流转发给浏览器 (SSE)。会话在 dsh 侧持久化,
      按账号映射 session-id → 永久记忆、各账号互相隔离。
安全: 入口 need_perm("ai"); dsh 引擎只在本机 spawn, 不直接对外。
"""
import asyncio
import json
import os
import queue
import shutil
import subprocess
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth import need_perm
import app_config
import db

router = APIRouter(prefix="/api/ai")

_ERR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_errors.log")

RUN_TIMEOUT = 900          # 单条消息的智能体运行上限 (秒), 超时强制终止
MSG_MAX = 8000             # 单条消息字符上限
_busy = set()              # 正在运行的用户 (每用户串行)


def _elog(m):
    """AI 模块错误落盘 (SYSTEM 会话无控制台, 不落盘就看不见)"""
    try:
        with open(_ERR_LOG, "a", encoding="utf-8") as f:
            f.write("[" + time.strftime("%m-%d %H:%M:%S") + "] " + str(m) + chr(10))
    except Exception:
        pass


# ---------------------------------------------------------------- dsh 定位与会话存取
def _dsh_bin():
    """定位全局安装的 dsh 入口 bin.js

    候选顺序: config 显式配置 (ai_dsh_bin) → 当前用户 npm 全局 → 本机部署的实际位置。
    禁止用 subprocess 探测 (npm 是 .cmd, 裸名字会 FileNotFoundError)。
    """
    global _DSH_BIN
    if _DSH_BIN and os.path.exists(_DSH_BIN):
        return _DSH_BIN
    sep = chr(92)
    cands = []
    if cfg_dsh_bin := app_config.load().get("ai_dsh_bin"):
        cands.append(cfg_dsh_bin)
    cands.append(os.path.expandvars("%APPDATA%" + sep + "npm" + sep + "node_modules"
                                    + sep + "@deepseek-ai" + sep + "dsh" + sep + "lib"
                                    + sep + "bin.js"))
    for cand in cands:
        if os.path.exists(cand):
            _DSH_BIN = cand
            return _DSH_BIN
    return None


_DSH_BIN = None


def _session_row(user):
    conn = db.get_conn()
    row = conn.execute("SELECT session_id, title FROM ai_sessions WHERE user=?", (user,)).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)


def _session_save(user, session_id, title):
    conn = db.get_conn()
    conn.execute("INSERT INTO ai_sessions (user, session_id, title, updated) VALUES (?,?,?,?) "
                 "ON CONFLICT(user) DO UPDATE SET session_id=excluded.session_id, "
                 "title=excluded.title, updated=excluded.updated",
                 (user, session_id, title, time.time()))
    conn.commit()
    conn.close()


def _session_clear(user):
    conn = db.get_conn()
    conn.execute("DELETE FROM ai_sessions WHERE user=?", (user,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 路由
@router.get("/state")
def ai_state(request: Request):
    user = need_perm(request, "ai")["username"]
    sid, title = _session_row(user)
    return {"session_id": sid, "title": title or "", "busy": user in _busy}


@router.post("/new")
async def ai_new(request: Request):
    """开启新对话: 清掉映射, 下一条消息会创建全新的 dsh 会话"""
    user = need_perm(request, "ai")["username"]
    if user in _busy:
        raise HTTPException(409, "上一条任务仍在执行")
    _session_clear(user)
    return {"ok": True}


@router.post("/chat")
async def ai_chat(request: Request):
    user = need_perm(request, "ai")["username"]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体编码异常 (需 UTF-8)")
    msg = (body.get("message") or "").strip()
    new_session = bool(body.get("new_session"))
    if not msg:
        raise HTTPException(400, "消息为空")
    if len(msg) > MSG_MAX:
        raise HTTPException(400, f"消息过长 (上限 {MSG_MAX} 字符)")
    if user in _busy:
        raise HTTPException(409, "上一条任务仍在执行, 请等它完成或刷新页面")

    try:
        cfg = app_config.load()
        api_key = cfg.get("ai_api_key") or ""
        if not api_key:
            raise HTTPException(400, "未配置 DeepSeek API 密钥 (config.ini [ai] ai_api_key)")

        if new_session:
            _session_clear(user)
        sid, _title = _session_row(user)
        title = msg[:24]

        env = dict(os.environ)
        env["DEEPSEEK_API_KEY"] = api_key

        binjs = _dsh_bin()
        if not binjs or not os.path.exists(binjs):
            _elog("dsh bin 未找到 (PATH 与候选路径均无)")
            raise HTTPException(500, "未找到 dsh 运行入口 (检查 npm 全局安装)")
    except HTTPException:
        raise
    except Exception as e:
        _elog("chat 准备失败: " + repr(e))
        raise HTTPException(500, "AI 初始化失败 (详见 ai_errors.log)")

    _busy.add(user)

    async def gen():
        q = queue.Queue()
        holder = {}

        # 同步线程跑 dsh 进程并按行投递事件 (Windows Selector 循环不支持 asyncio 子进程)
        def run_dsh():
            try:
                for attempt in (1, 2):
                    use_sid = sid if (attempt == 1 and sid) else None
                    pargs = ["node", binjs, "--profile", "headless", "--json"]
                    if use_sid:
                        pargs += ["--session-id", use_sid]
                    pargs.append(msg)
                    proc = subprocess.Popen(
                        pargs,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=r"D:\Monitor", env=env,
                        text=True, encoding="utf-8", errors="ignore")
                    holder["proc"] = proc
                    session_missing = False
                    for line in proc.stdout:
                        line = line.rstrip()
                        if "does not exist" in line:
                            session_missing = True
                        q.put(("line", line))
                    proc.wait()
                    if session_missing and attempt == 1:
                        q.put(("note", "stored session missing, retrying without session-id"))
                        continue
                    q.put(("exit", proc.returncode))
                    return
            except Exception as e:
                q.put(("spawn_error", str(e)))

        threading.Thread(target=run_dsh, daemon=True).start()

        new_sid = None
        final_text = None
        err_text = None
        deadline = time.time() + RUN_TIMEOUT
        try:
            yield 'data: {"type":"start"}\n\n'
            while True:
                if time.time() > deadline:
                    p = holder.get("proc")
                    if p and p.poll() is None:
                        p.kill()
                    yield 'data: {"type":"error","error":"任务超时被终止"}\n\n'
                    break
                try:
                    kind, val = await asyncio.wait_for(
                        asyncio.to_thread(q.get, True, 1), timeout=5)
                except queue.Empty:
                    continue
                if kind == "spawn_error":
                    yield 'data: {"type":"error","error":' + json.dumps(val, ensure_ascii=False) + '}\n\n'
                    break
                if kind == "line":
                    try:
                        ev = json.loads(val)
                    except ValueError:
                        continue
                    et = ev.get("type")
                    if et == "session" and not sid:
                        new_sid = ev.get("sessionId")
                    elif et == "final":
                        final_text = ev.get("text") or ""
                        reason = (ev.get("reason") or {})
                        if reason.get("kind") == "error":
                            err_text = (reason.get("error") or {}).get("message") or "未知错误"
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    if et == "final":
                        break
                elif kind == "exit":
                    break
            if new_sid:
                _session_save(user, new_sid, title)
            if err_text:
                yield 'data: {"type":"error","error":' + json.dumps(err_text, ensure_ascii=False) + '}\n\n'
            yield "data: [DONE]\n\n"
        except Exception as e:
            _elog("gen 异常: " + repr(e))
            try:
                yield 'data: {"type":"error","error":"内部错误 (详见 ai_errors.log)"}\n\n'
            except Exception:
                pass
        finally:
            _busy.discard(user)
            p = holder.get("proc")
            if p and p.poll() is None:
                p.kill()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
