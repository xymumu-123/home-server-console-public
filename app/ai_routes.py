# -*- coding: utf-8 -*-
"""AI 助手: dsh (DeepSeek Harness) Web UI 的鉴权反向代理

架构: 控制台 AI 磁贴/导航 → /ai/* (本模块: 登录 + ai 权限位门禁)
      → 127.0.0.1:3080 (dsh web 常驻引擎, HTTP + WebSocket 透传)。
dsh web 只绑回环, 永不直接对外; 引擎的工具 = 服务器 shell (全能力)。
旧的自建聊天/headless 驱动已由 dsh 官方 Web UI 取代。
"""
import asyncio
import os

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse
from httpx_ws import WebSocketDisconnect as HttpxWSDisconnect
from httpx_ws import aconnect_ws

from auth import need_perm, check_token, get_user
import app_config

router = APIRouter(prefix="/ai")

UPSTREAM = "http://127.0.0.1:3080"

# 逐跳头 (RFC 7230: 代理不转发)。注意 host/cookie 刻意转发:
# dsh 信任围栏按 Host+Origin 校验, 其签名 cookie 是上游鉴权凭据。
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade",
               "content-length", "accept-encoding"}


def _ai_perm_user(request_or_ws):
    """会话 cookie → 用户; 无 ai 权限位返回 None"""
    try:
        token = request_or_ws.cookies.get("sid") or ""
    except Exception:
        return None
    username = check_token(token)
    if not username:
        return None
    u = get_user(username)
    if not u or (not u.get("is_admin") and "ai" not in (u.get("perms") or [])):
        return None
    return u


# ---------------------------------------------------------------- HTTP 反代
# 注意: 这里只挂 "" (即 /ai 无斜杠); /ai/ 必须落进下方 {path:path} 通配透传,
# 若此处再挂 "/" 会把 /ai/ 重定向回 /ai/ 造成自旋。
@router.get("")
async def ai_root(request: Request):
    if not _ai_perm_user(request):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/ai/", status_code=307)


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE",
                                            "PATCH", "OPTIONS", "HEAD"])
async def ai_proxy(path: str, request: Request):
    user = _ai_perm_user(request)
    if not user:
        if _looks_like_browser(request):
            return RedirectResponse("/login", status_code=303)
        raise HTTPException(401, "unauth")

    target = UPSTREAM + "/" + path
    if request.url.query:
        target += "?" + request.url.query

    # 浏览器原始头基本原样转发 (含 Host/Cookie/Origin):
    # dsh 的信任围栏按 Host+Origin 校验, 其签名 cookie 也必须能送达上游。
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_HEADERS}
    body = await request.body()

    client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=300))
    try:
        up = await client.send(
            client.build_request(request.method, target, headers=headers,
                                 content=body if body else None),
            stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"dsh 引擎未响应: {e}")

    # 首次访问: dsh 未认得浏览器 (无其签名 cookie) → 401。
    # 从引擎日志取最新 token 自动补跳, 浏览器侧一次性铸 cookie, 用户无感。
    if up.status_code == 401 and _looks_like_browser(request) \
            and "token=" not in (request.url.query or ""):
        await up.aclose()
        await client.aclose()
        tok = _latest_dsh_token()
        if tok:
            sep = "&" if request.url.query else ""
            return RedirectResponse(
                f"/ai/{path}?{request.url.query}{sep}token={tok}", status_code=302)
        raise HTTPException(502, "dsh 引擎在线但未取得访问 token, 检查 logs/dsh-web.log")

    async def stream():
        try:
            # aiter_bytes = httpx 已按协商编码解压; 响应头里 content-encoding 已剥
            async for chunk in up.aiter_bytes():
                yield chunk
        finally:
            await up.aclose()
            await client.aclose()

    resp_headers = {k: v for k, v in up.headers.items()
                    if k.lower() not in HOP_HEADERS and k.lower() != "content-encoding"}
    return StreamingResponse(stream(), status_code=up.status_code,
                             headers=resp_headers, media_type=up.headers.get("content-type"))


def _looks_like_browser(request: Request):
    return "text/html" in (request.headers.get("accept") or "")


def _wslog(msg):
    """临时调试: WS 泵帧计数与关闭码 -> logs/ai-ws-debug.log"""
    import time as _t
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "logs", "ai-ws-debug.log"), "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(_t.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _latest_dsh_token():
    """从 dsh-web.log 取最新一次启动打印的访问 token (每次引擎启动会更换)"""
    import re
    try:
        log = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "dsh-web.log")
        m = None
        with open(log, encoding="utf-8", errors="ignore") as f:
            for line in f:
                mm = re.search(r"\?token=([A-Za-z0-9_\-]+)", line)
                if mm:
                    m = mm
        return m.group(1) if m else None
    except Exception:
        return None


# ---------------------------------------------------------------- WebSocket 反代
# 上游 WS 客户端用 httpx-ws: dsh 的签名 cookie 绑定浏览器 authority (host:port),
# 上游握手时 Host 必须与之一致才放行; websockets 库不支持 Host 覆盖, httpx 支持。
@router.websocket("/{path:path}")
async def ai_ws_proxy(ws: WebSocket, path: str):
    user = _ai_perm_user(ws)
    if not user:
        await ws.close(code=1008)
        return
    query = ("?" + ws.url.query) if ws.url.query else ""
    await ws.accept()
    close_code = 1000
    try:
        headers = {"Cookie": ws.headers.get("cookie") or ""}
        # 保持浏览器原始 authority, 否则 dsh 按 cookie 绑定域拒绝 (401 unauthorized)
        for h in ("host", "origin"):
            v = ws.headers.get(h)
            if v:
                headers["Host" if h == "host" else "Origin"] = v
        _wslog("open path=/{} host={} ck={}".format(path, ws.headers.get("host"), bool(ws.headers.get("cookie"))))
        async with httpx.AsyncClient(base_url=UPSTREAM, timeout=None) as client:
            async with aconnect_ws(UPSTREAM + "/" + path + query, client,
                                   headers=headers) as up:

                async def c2u():
                    n = 0
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                _wslog("c2u client-disconnect after {} frames".format(n))
                                break
                            if msg.get("text") is not None:
                                n += 1
                                await up.send_text(msg["text"])
                            elif msg.get("bytes") is not None:
                                n += 1
                                await up.send_bytes(msg["bytes"])
                    except Exception as e:
                        _wslog("c2u exc {} after {} frames: {}".format(type(e).__name__, n, str(e)[:80]))

                async def u2c():
                    nonlocal close_code
                    n = 0
                    try:
                        while True:
                            m = await up.receive()
                            if isinstance(m.data, str):
                                n += 1
                                await ws.send_text(m.data)
                            else:
                                n += 1
                                await ws.send_bytes(m.data)
                    except HttpxWSDisconnect as e:
                        close_code = e.code
                        _wslog("u2c upstream-disconnect code={} after {} frames".format(e.code, n))
                        raise
                    except Exception as e:
                        _wslog("u2c exc {} after {} frames: {}".format(type(e).__name__, n, str(e)[:100]))
                        raise

                done, pending = await asyncio.wait(
                    [asyncio.create_task(c2u()), asyncio.create_task(u2c())],
                    return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
    except Exception:
        pass
    try:
        await ws.close(code=close_code)
    except Exception:
        pass
