# -*- coding: utf-8 -*-
"""链接分享: 创建/管理/公开访问页/下载/在线播放

提取码不走 URL 查询参数 (会进浏览器历史与访问日志), 改为:
  1. 打开 /s/{token} → 需要提取码时渲染 POST 表单
  2. POST /s/{token} 提交提取码 → 校验通过后种下 1 小时的签名 Cookie → 303 回页面
  3. 之后 /s/{token}、/s/{token}/dl、/s/{token}/stream 均凭 Cookie 放行, URL 干净
"""
import hashlib
import hmac
import html as html_mod
import os
import secrets
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from auth import need_perm, SECRET
from db import get_conn
from fs_routes import safe_path, stream_core

router = APIRouter()

_share_rate = {}

COOKIE_TTL = 3600          # 提取通过后的 Cookie 有效期 (秒)


def _rate_ok(ip: str) -> bool:
    """公开分享接口的简易限速: 每 IP 每分钟 90 次"""
    now = time.time()
    rec = _share_rate.get(ip)
    if not rec or now - rec[1] > 60:
        if len(_share_rate) > 500:
            for k in [k for k, v in _share_rate.items() if now - v[1] > 60]:
                _share_rate.pop(k, None)
        _share_rate[ip] = [1, now]
        return True
    rec[0] += 1
    return rec[0] <= 90


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _share_sig(token: str, exp: int) -> str:
    return hmac.new(SECRET, f"share:{token}:{exp}".encode(), hashlib.sha256).hexdigest()


def _cookie_name(token: str) -> str:
    return "sh_" + _token_hash(token)[:16]


def _cookie_ok(token: str, request: Request) -> bool:
    """提取通过后种下的签名 Cookie 是否有效"""
    raw = request.cookies.get(_cookie_name(token)) or ""
    try:
        exp_s, sig = raw.split(":", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _share_sig(token, exp))


def _grant_cookie(resp, token: str):
    exp = int(time.time()) + COOKIE_TTL
    resp.set_cookie(_cookie_name(token), f"{exp}:{_share_sig(token, exp)}",
                    max_age=COOKIE_TTL, httponly=True, samesite="lax")


def _get_share(token: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, path, label, expires, pw_hash, downloads FROM shares WHERE token_hash = ?",
        (_token_hash(token),)).fetchone()
    conn.close()
    return row


def _share_valid(row) -> bool:
    return bool(row) and (row[3] == 0 or row[3] > time.time())


def _share_page_html(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_mod.escape(title)}</title>
<style>
  body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; background: #eef1f6; color: #1e293b;
          min-height: 100vh; display: flex; align-items: center; justify-content: center; margin: 0; padding: 20px; }}
  .card {{ background: #fff; border: 1px solid #e4e9f2; border-radius: 18px; padding: 36px 40px;
           text-align: center; box-shadow: 0 12px 28px rgba(15,23,42,.10); max-width: 430px; width: 100%; }}
  .icon {{ font-size: 42px; margin-bottom: 10px; }}
  h1 {{ font-size: 16px; margin: 6px 0 8px; word-break: break-all; }}
  .meta {{ font-size: 12px; color: #64748b; margin-bottom: 20px; }}
  .err {{ color: #dc2626; font-size: 13px; margin: -8px 0 10px; }}
  .btn {{ display: block; text-decoration: none; background: #2f6fed; color: #fff; padding: 12px;
          border-radius: 10px; font-size: 15px; font-weight: 600; margin-top: 10px; }}
  .btn.ghost {{ background: #eaf1fe; color: #2f6fed; }}
  input {{ width: 100%; padding: 11px 12px; border-radius: 10px; border: 1px solid #e4e9f2;
           font-size: 14px; margin-bottom: 6px; box-sizing: border-box; }}
  button {{ width: 100%; padding: 11px; border: none; border-radius: 10px; background: #2f6fed;
            color: #fff; font-size: 14px; font-weight: 600; cursor: pointer; }}
  .foot {{ font-size: 11px; color: #94a3b8; margin-top: 18px; }}
</style></head><body>
<div class="card">{body}<div class="foot">来自 家庭服务器中控台 的分享</div></div>
</body></html>"""


def _pw_form(token: str, err: str = "") -> HTMLResponse:
    err_html = f"<p class='err'>{html_mod.escape(err)}</p>" if err else ""
    form = f"""<form method="post" action="/s/{token}">
                 <input name="pw" placeholder="请输入提取码" required autofocus>
                 {err_html}<button type="submit">确 定</button>
               </form>"""
    return HTMLResponse(_share_page_html("提取文件", f"<div class='icon'>🔒</div><h1>此分享需要提取码</h1>{form}"))


def _fmt_size(b) -> str:
    u = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    b = float(b or 0)
    while b >= 1024 and i < len(u) - 1:
        b /= 1024
        i += 1
    return f"{b:.1f} {u[i]}"


@router.post("/api/share/create")
async def share_create(request: Request):
    need_perm(request, "files")
    body = await request.json()
    rp = safe_path(body.get("path") or "")
    if not os.path.isfile(rp):
        raise HTTPException(404, "not a file")
    try:
        days = int(body.get("days"))
    except (TypeError, ValueError):
        days = 7
    if days not in (0, 1, 7, 30):
        days = 7
    password = str(body.get("password") or "").strip()
    token = secrets.token_urlsafe(12)
    conn = get_conn()
    conn.execute(
        "INSERT INTO shares (token, token_hash, path, label, created, expires, pw_hash) VALUES (?,?,?,?,?,?,?)",
        (token, _token_hash(token), rp, os.path.splitext(os.path.basename(rp))[0],
         time.time(), time.time() + days * 86400 if days else 0,
         hashlib.sha256(password.encode()).hexdigest() if password else ""))
    conn.commit()
    conn.close()
    return {"token": token, "days": days, "has_password": bool(password)}


@router.get("/api/shares")
def share_list(request: Request):
    need_perm(request, "files")
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, token, path, label, created, expires, downloads, pw_hash FROM shares ORDER BY created DESC").fetchall()
    conn.close()
    now = time.time()
    return {"shares": [{"id": r[0], "token": r[1], "path": r[2], "label": r[3], "created": r[4],
                        "expires": r[5], "downloads": r[6],
                        "has_password": bool(r[7]),
                        "expired": bool(r[5] and r[5] < now)} for r in rows]}


@router.post("/api/share/revoke")
async def share_revoke(request: Request):
    need_perm(request, "files")
    body = await request.json()
    conn = get_conn()
    conn.execute("DELETE FROM shares WHERE id = ?", (int(body.get("id") or 0),))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------- 公开访问 (无需登录)
@router.post("/s/{token}")
async def share_pw_submit(token: str, request: Request):
    """提取码表单提交: 校验通过 → 种签名 Cookie → 303 回访问页"""
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip):
        return HTMLResponse(_share_page_html("稍后再试", "<p>请求过于频繁</p>"), status_code=429)
    row = _get_share(token)
    if not _share_valid(row):
        return HTMLResponse(_share_page_html("链接已失效", "<p class='meta'>链接不存在或已过期</p>"), status_code=404)
    form = parse_qs((await request.body()).decode("utf-8", "ignore"))
    pw = form.get("pw", [""])[0]
    if row[4] and not hmac.compare_digest(_token_hash(pw or ""), row[4]):
        return _pw_form(token, "提取码错误, 请重试")
    resp = RedirectResponse(f"/s/{token}", status_code=303)
    _grant_cookie(resp, token)
    return resp


@router.get("/s/{token}")
def share_page(token: str, request: Request):
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip):
        return HTMLResponse(_share_page_html("稍后再试", "<p>请求过于频繁</p>"), status_code=429)
    row = _get_share(token)
    if not _share_valid(row):
        return HTMLResponse(_share_page_html("链接已失效", "<p class='meta'>链接不存在或已过期</p>"), status_code=404)
    if row[4] and not _cookie_ok(token, request):
        return _pw_form(token)
    path, label = row[1], row[2]
    ext = os.path.splitext(path)[1].lower()
    is_video = ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")

    try:
        size_text = _fmt_size(os.path.getsize(path))
    except OSError:
        size_text = "-"
    expire_text = "永久有效" if not row[3] else "有效期至 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(row[3]))
    play_btn = (f"<a class='btn ghost' href='/s/{token}/stream' target='_blank'>▶ 在线播放</a>"
                if is_video else "")
    body = f"""<div class="icon">{"🎬" if is_video else "📄"}</div>
      <h1>{html_mod.escape(label)}</h1>
      <div class="meta">{size_text} · {expire_text}</div>
      <a class="btn" href="/s/{token}/dl">⬇ 下载文件</a>
      {play_btn}"""
    return HTMLResponse(_share_page_html(label, body))


def _share_guard(token: str, request: Request):
    """公开下载/播放的公共校验: 限速+存在+未过期+提取 Cookie。
    通过校验返回分享行; 有提取码但未通过时返回 303 跳转 (调方可直接 return)"""
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip):
        raise HTTPException(429, "请求过于频繁")
    row = _get_share(token)
    if not _share_valid(row):
        raise HTTPException(404, "链接不存在或已过期")
    if row[4] and not _cookie_ok(token, request):
        # 未通过提取: 引到访问页补提取码 (直接 401 会让访客困惑)
        return RedirectResponse(f"/s/{token}", status_code=303)
    return row


@router.get("/s/{token}/dl")
def share_download(token: str, request: Request):
    row = _share_guard(token, request)
    if isinstance(row, RedirectResponse):
        return row
    if not os.path.isfile(row[1]):
        raise HTTPException(404, "文件已不存在")
    conn = get_conn()
    conn.execute("UPDATE shares SET downloads = downloads + 1 WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    return FileResponse(row[1], filename=os.path.basename(row[1]))


@router.get("/s/{token}/stream")
def share_stream(token: str, request: Request):
    row = _share_guard(token, request)
    if isinstance(row, RedirectResponse):
        return row
    if not os.path.isfile(row[1]):
        raise HTTPException(404, "文件已不存在")
    return stream_core(request, row[1], 0)
