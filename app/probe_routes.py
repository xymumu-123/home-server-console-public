# -*- coding: utf-8 -*-
"""连通性探针: 供浏览器跨源检测 IPv4/IPv6 连通性与带宽测速。

- 免鉴权 (跨源 fetch 带不了登录 cookie), 只暴露无敏感信息的探针
- 全部带 CORS 头 (Access-Control-Allow-Origin: *), 支持从 pc2 域名页探测 frp 线路、反之亦然
- 简易限速: ping/info 120 次/分/IP, down/up 30 次/分/IP (单次下载最大 25MB, 上传 25MB)
"""
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from auth import CFG
import ddns

router = APIRouter(prefix="/api/probe")

CHUNK = 64 * 1024
DOWN_MAX_KB = int(CFG.get("down_max_mb", 25)) * 1024
UP_MAX = int(CFG.get("up_max_mb", 25)) * 1024 * 1024

_rate = {}
_vol = {}


def _rate_ok(ip: str, limit: int) -> bool:
    now = time.time()
    rec = _rate.get(ip)
    if not rec or now - rec[1] > 60:
        if len(_rate) > 2000:
            for k in [k for k, v in _rate.items() if now - v[1] > 60]:
                _rate.pop(k, None)
        _rate[ip] = [1, now]
        return True
    rec[0] += 1
    return rec[0] <= limit


def _vol_ok(ip: str, add: int, max_mb: int = None) -> bool:
    """测速大流量按体积限速: 每 IP 每分钟 3GB (千兆内网完整测速约需 2GB, 滥用仍有上限)"""
    now = time.time()
    if max_mb is None:
        max_mb = int(CFG.get("volume_limit_mb", 3072))
    rec = _vol.get(ip)
    if not rec or now - rec[1] > 60:
        if len(_vol) > 2000:
            for k in [k for k, v in _vol.items() if now - v[1] > 60]:
                _vol.pop(k, None)
        _vol[ip] = [add, now]
        return True
    rec[0] += add
    return rec[0] <= max_mb * 1024 * 1024


def _cors(body, status=200, extra=None):
    h = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
    if extra:
        h.update(extra)
    return Response(body, status_code=status, headers=h)


@router.options("/{rest:path}")
async def probe_options(rest: str):
    """预检兜底 (常规 GET/裸 POST 不会触发, 但带上无妨)"""
    return _cors(b"", status=204)


@router.get("/ping")
def probe_ping(request: Request):
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip, int(CFG.get("rate_per_min", 120))):
        return _cors(b'{"ok":false,"err":"rate"}', status=429)
    return _cors('{"ok":true,"t":%.3f}' % time.time())


V6_PORT = int(CFG.get("v6_port", 18002))   # 用户面 IPv6 端口 (portproxy ::18002 → 8000)


@router.get("/info")
def probe_info(request: Request):
    """当前站点的线路信息: 服务器 IPv6 字面量 + frp 备用地址 (供另一条线路的跨源探测)"""
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip, 120):
        return _cors(b'{"err":"rate"}', status=429)
    try:
        v6 = ddns.status().get("ip")
    except Exception:
        v6 = None
    frp = CFG.get("frp_url") or ""
    domain = (CFG.get("ddns") or {}).get("domain", "")
    v6_scheme = "https" if (CFG.get("ssl_cert") and CFG.get("ssl_key")) else "http"
    v6_origin = (v6_scheme + '://' + domain + ':' + str(V6_PORT)) if domain else ""
    return _cors('{"v6":%s,"v6_port":%d,"frp_url":%s,"v6_origin":%s}' % (
        ('"%s"' % v6) if v6 else "null", V6_PORT,
        ('"%s"' % frp) if frp else "null",
        ('"%s"' % v6_origin) if v6_origin else "null"))


@router.get("/down")
def probe_down(request: Request, kb: int = 1024):
    """流式下发 kb KB 的零数据 (测下载带宽); 上限 25MB"""
    ip = request.client.host if request.client else "?"
    kb = max(64, min(kb, DOWN_MAX_KB))
    if not _vol_ok(ip, kb * 1024):
        return _cors(b'{"err":"rate"}', status=429)
    total = kb * 1024
    chunk = b"\x00" * CHUNK

    def gen():
        sent = 0
        while sent < total:
            n = min(CHUNK, total - sent)
            yield chunk[:n]
            sent += n

    return StreamingResponse(gen(), media_type="application/octet-stream", headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "Content-Length": str(total),
    })


@router.post("/up")
async def probe_up(request: Request):
    """接收并丢弃上传数据 (测上传带宽); 上限 25MB"""
    ip = request.client.host if request.client else "?"
    if not _vol_ok(ip, 0):
        return _cors(b'{"err":"rate"}', status=429)
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > UP_MAX or not _vol_ok(ip, len(chunk)):
            return _cors(b'{"err":"too large"}', status=413)
    return _cors('{"ok":true,"bytes":%d,"t":%.3f}' % (total, time.time()))
