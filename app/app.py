# -*- coding: utf-8 -*-
"""
家庭服务器监控站 — 应用装配层
- 页面路由 / 登录 / 监控 API / 设置 / 人员管理 / DDNS 入口留在本文件
- 各业务域拆分: fs_routes(文件) games(游戏) probe_routes(探针) share_routes(分享)
  dl_routes(下载机) power_routes(电源/WOL) media_routes(影视) db(SQLite)
数据源: psutil (Windows 本机) + LibreHardwareMonitor Web 服务 (温度/功耗)
"""
import json
import os
import threading
import time

import psutil
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from auth import (CFG, make_token, is_authed, login_throttled,
                  record_fail, clear_fail, need_perm, verify_login,
                  list_users, create_user, update_perms, reset_password,
                  delete_user, migrate_admin)
from db import get_conn, init_db, kv_get, kv_set
from fs_routes import router as fs_router, fixed_drives
from probe_routes import router as probe_router
from share_routes import router as share_router
from dl_routes import router as dl_router, aria2_maintenance
from power_routes import router as power_router
from ai_routes import router as ai_router
from media_routes import router as media_router
import app_config
import ddns
import games

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

class NoCacheStaticFiles(StaticFiles):
    """静态文件禁缓存: 改版后浏览器立即拿到新版 (304 验证成本可忽略)"""
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app = FastAPI()
app.mount("/static", NoCacheStaticFiles(directory=STATIC), name="static")
app.include_router(fs_router)
app.include_router(games.router)
app.include_router(probe_router)
app.include_router(share_router)
app.include_router(dl_router)
app.include_router(power_router)
app.include_router(media_router)
app.include_router(ai_router)

init_db()
migrate_admin()          # config.json 明文 admin 密码 → users 表 (幂等)


@app.middleware("http")
async def hsts_header(request: Request, call_next):
    """HSTS: 浏览器记住后, 任何 http://pc2.jzlspace.top:<任意端口> 的访问都会被
    自动改写为 https (解决地址栏默认猜 http 导致打不开的问题)。
    浏览器规范规定 HTTP 响应里的 HSTS 会被忽略, 所以无条件加是安全的。
    30 天有效期, 每次 https 访问自动续; 回滚 HTTP 时需在浏览器清除 HSTS 记录。"""
    resp = await call_next(request)
    resp.headers["Strict-Transport-Security"] = "max-age=2592000"
    return resp


# ---------------------------------------------------------------- 传感器数据
SENSOR_FILE = os.path.abspath(os.path.join(BASE, "..", "sensors.json"))


def read_sensors():
    """读取 sensor_daemon.ps1 写入的传感器数据; 超过 10 秒未更新视为离线"""
    result = {
        "lhm_ok": False,
        "cpu_temp": None, "mb_temp": None, "cpu_power": None,
        "temps": [], "powers": [], "fans": [],
    }
    try:
        if time.time() - os.stat(SENSOR_FILE).st_mtime > 10:
            return result
        with open(SENSOR_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return result

    result["lhm_ok"] = True
    result["cpu_temp"] = data.get("cpu_temp")
    result["cpu_power"] = data.get("cpu_power")
    # 物理盘健康 (SMART): 每盘 温度/通电时长/健康状态/累计读写 (sensor_daemon 每 60s 刷新计数器)
    result["disks_smart"] = data.get("disks") or []

    zones = data.get("acpi_zones") or []
    if zones:
        result["mb_temp"] = max(z.get("value") or 0 for z in zones)
        for z in zones:
            name = (z.get("name") or "").split("\\")[-1]
            result["temps"].append(("主板热区/" + name, z.get("value")))

    for s in data.get("sensors") or []:
        stype = s.get("type")
        name = s.get("name", "")
        v = s.get("value")
        if v is None:
            continue
        if stype == "Temperature":
            result["temps"].append((name, v))
        elif stype == "Power":
            result["powers"].append((name, v))
        elif stype == "Fan":
            result["fans"].append((name, v))
    return result


# ---------------------------------------------------------------- 采集线程
_AGG_COLS = ("cpu", "mem", "cpu_temp", "mb_temp", "cpu_power", "est_power", "net_up", "net_down")
_ROLLUP_TIERS = [  # (源表, 源表保留秒数, 目标表, 聚合粒度秒)
    ("samples", 24 * 3600, "r1m", 60),      # 24小时前: 2s → 1分钟
    ("r1m", 7 * 86400, "r15m", 900),        # 7天前: 1分钟 → 15分钟
    ("r15m", 30 * 86400, "r1h", 3600),      # 30天前: 15分钟 → 1小时
]


def rollup(conn, now):
    """把超过上一级保留期的数据聚合成更粗粒度, 聚合后删除源数据 (幂等)。
    cutoff 按目标粒度对齐, 保证被聚合的桶是完整的"""
    aggs = ", ".join(f"AVG({c})" for c in _AGG_COLS)
    for src, keep, dst, gran in _ROLLUP_TIERS:
        cutoff = ((now - keep) // gran) * gran
        conn.execute(
            f"INSERT OR REPLACE INTO {dst} "
            f"SELECT CAST(ts/{gran} AS INTEGER)*{gran}, {aggs} "
            f"FROM {src} WHERE ts < ? GROUP BY 1",
            (cutoff,),
        )
        conn.execute(f"DELETE FROM {src} WHERE ts < ?", (cutoff,))
    long_keep = CFG.get("long_retention_days", 365) * 86400
    conn.execute("DELETE FROM r1h WHERE ts < ?", (now - long_keep,))


_top_procs = []  # [{pid, name, cpu, mem_mb}] 按CPU排序


def update_top_procs():
    out = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            mi = p.info["memory_info"]
            out.append({"pid": p.pid, "name": (p.info["name"] or "?")[:30],
                        "cpu": p.cpu_percent(None),
                        "mem_mb": round(mi.rss / 1048576, 1) if mi else 0.0})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    out.sort(key=lambda x: x["cpu"], reverse=True)
    global _top_procs
    _top_procs = out[:6]


def sampler():
    interval = CFG.get("sample_interval", 2)
    conn = get_conn()
    energy_wh = float(kv_get(conn, "energy_wh") or 0.0)
    last_ts = time.time()
    prev_io = psutil.net_io_counters()
    last_rollup = 0.0

    while True:
        time.sleep(interval)
        try:
            now = time.time()
            dt = now - last_ts
            last_ts = now

            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            io = psutil.net_io_counters()
            net_up = max(0.0, (io.bytes_sent - prev_io.bytes_sent) / dt)
            net_down = max(0.0, (io.bytes_recv - prev_io.bytes_recv) / dt)
            prev_io = io

            lhm = read_sensors()
            cpu_power = lhm["cpu_power"]
            est_power = None
            if cpu_power is not None:
                est_power = cpu_power + CFG.get("baseline_power_w", 15)
                energy_wh += est_power * dt / 3600.0
                kv_set(conn, "energy_wh", f"{energy_wh:.4f}")
            kv_set(conn, "lhm_ok", "1" if lhm["lhm_ok"] else "0")

            conn.execute(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
                (now, cpu, mem.percent, lhm["cpu_temp"], lhm["mb_temp"],
                 cpu_power, est_power, net_up, net_down),
            )

            update_top_procs()
            aria2_maintenance()            # 下载机保活 + tracker 注入 (dl_routes)

            if now - last_rollup > 600:
                rollup(conn, now)
                conn.execute("DELETE FROM shares WHERE expires > 0 AND expires < ?", (now,))
                last_rollup = now
            conn.commit()
        except Exception as e:
            print(f"[sampler] {e}", flush=True)


# ---------------------------------------------------------------- 页面
def serve_page(filename):
    # 页面禁缓存: 改版后浏览器立即拿到新版 (静态资源仍走默认缓存)
    return FileResponse(os.path.join(STATIC, filename),
                        headers={"Cache-Control": "no-cache"})


@app.get("/monitor")
def monitor_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("index.html")


@app.get("/")
def control_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("control.html")


@app.get("/files")
def files_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("files.html")


@app.get("/media")
def media_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("media.html")


@app.get("/games")
def games_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("games.html")


@app.get("/ai")
def ai_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("ai.html")


@app.get("/downloads")
def downloads_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return serve_page("downloads.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return serve_page("login.html")


@app.post("/login")
async def do_login(request: Request):
    from urllib.parse import parse_qs
    client = request.client.host if request.client else "unknown"
    form = parse_qs((await request.body()).decode("utf-8", "ignore"))
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    # 节流 key 含用户名: 攻击者爆破某账号不会锁死同 IP 的其他账号
    key = f"{client}:{username}"
    if login_throttled(key):
        return RedirectResponse("/login?err=2", status_code=303)
    if verify_login(username, password):
        clear_fail(key)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("sid", make_token(username), max_age=30 * 86400,
                        httponly=True, samesite="lax")
        return resp
    record_fail(key)
    return RedirectResponse("/login?err=1", status_code=303)


@app.get("/api/current")
def api_current(request: Request):
    need_perm(request)
    conn = get_conn()
    row = conn.execute(
        "SELECT ts, cpu, mem, cpu_temp, mb_temp, cpu_power, est_power,"
        " net_up, net_down FROM samples ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    lhm = read_sensors()
    energy_wh = kv_get(conn, "energy_wh")
    conn.close()

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disks = []
    for d in fixed_drives():
        try:
            u = psutil.disk_usage(d["path"])
            disks.append({"path": d["path"].rstrip("\\") + "\\",
                          "total": u.total, "used": u.used,
                          "percent": u.percent})
        except Exception:
            pass
    boot = psutil.boot_time()
    return {
        "ts": time.time(),
        "cpu": row[1] if row else psutil.cpu_percent(),
        "mem": row[2] if row else mem.percent,
        "mem_used": mem.used, "mem_total": mem.total,
        "swap_percent": swap.percent,
        "cpu_temp": row[3] if row else None,
        "mb_temp": row[4] if row else None,
        "cpu_power": row[5] if row else None,
        "est_power": row[6] if row else None,
        "net_up": row[7] if row else 0,
        "net_down": row[8] if row else 0,
        "energy_kwh": round(float(energy_wh) / 1000, 4) if energy_wh else 0,
        "lhm_ok": lhm["lhm_ok"],
        "temps_all": lhm["temps"], "fans": lhm["fans"],
        "disks_smart": lhm.get("disks_smart", []),
        "disk_unhealthy": any(
            (d.get("health") or "").lower() not in ("", "healthy", "unknown")
            for d in lhm.get("disks_smart", [])),
        "disks": disks,
        "uptime_s": time.time() - boot,
        "cpu_count": psutil.cpu_count(),
        "cpu_name": platform_cpu_name(),
        "top_procs": _top_procs,
        "quick_links": CFG.get("quick_links", []),
    }


def platform_cpu_name():
    try:
        return os.environ.get("PROCESSOR_IDENTIFIER", "") or "CPU"
    except Exception:
        return "CPU"


@app.get("/api/history")
def api_history(request: Request, minutes: int = 1440):
    need_perm(request)
    max_min = CFG.get("long_retention_days", 365) * 1440
    minutes = max(5, min(minutes, max_min))
    since = time.time() - minutes * 60
    # 按时间范围选数据源: 范围越大用越粗的聚合表
    if minutes <= 1440:
        table = "samples"
    elif minutes <= 7 * 1440:
        table = "r1m"
    elif minutes <= 30 * 1440:
        table = "r15m"
    else:
        table = "r1h"
    bucket = max(1, minutes * 60 // 720)  # 最多约 720 个点
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT CAST(ts/{bucket} AS INTEGER)*{bucket},
                   AVG(cpu), AVG(mem), AVG(cpu_temp), AVG(mb_temp),
                   AVG(cpu_power), AVG(est_power), AVG(net_up), AVG(net_down)
            FROM {table} WHERE ts > ? GROUP BY 1 ORDER BY 1""",
        (since,),
    ).fetchall()
    conn.close()
    return {"bucket": bucket,
            "rows": [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]]
                     for r in rows]}


# ---------------------------------------------------------------- 设置
@app.get("/api/settings")
def get_settings(request: Request):
    need_perm(request, "system")
    return {"media_dirs": CFG.get("media_dirs", []),
            "game_dirs": CFG.get("game_dirs", [])}


@app.post("/api/settings")
async def save_settings(request: Request):
    """保存扫描目录设置: 校验存在性+去重, 内存即时生效, 原子写回配置文件"""
    need_perm(request, "system")
    body = await request.json()

    def clean_dirs(lst):
        out, seen = [], set()
        for p in lst or []:
            p = str(p).strip()
            if not p:
                continue
            rp = os.path.normpath(os.path.realpath(p))
            if not os.path.isdir(rp):
                raise HTTPException(400, f"目录不存在: {p}")
            if rp.lower() not in seen:
                seen.add(rp.lower())
                out.append(rp)
        return out

    CFG["media_dirs"] = clean_dirs(body.get("media_dirs"))
    CFG["game_dirs"] = clean_dirs(body.get("game_dirs"))
    app_config.save_ui_keys(CFG)
    return {"ok": True, "media_dirs": CFG["media_dirs"], "game_dirs": CFG["game_dirs"]}


# ---------------------------------------------------------------- 当前账号与人员管理
@app.get("/api/me")
def api_me(request: Request):
    u = need_perm(request)                     # 登录即可
    return {"username": u["username"], "perms": u["perms"], "is_admin": u["is_admin"]}


@app.get("/api/users")
def api_users_list(request: Request):
    need_perm(request, "users")
    return {"users": list_users()}


@app.post("/api/users")
async def api_users_create(request: Request):
    need_perm(request, "users")
    body = await request.json()
    try:
        create_user(str(body.get("username") or ""), str(body.get("password") or ""),
                    body.get("perms") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/users/perms")
async def api_users_perms(request: Request):
    need_perm(request, "users")
    body = await request.json()
    try:
        update_perms(str(body.get("username") or ""), body.get("perms") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/users/password")
async def api_users_password(request: Request):
    """重置密码 (人员管理权限); admin 也可重置自己的"""
    need_perm(request, "users")
    body = await request.json()
    try:
        reset_password(str(body.get("username") or ""), str(body.get("password") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/users/delete")
async def api_users_delete(request: Request):
    need_perm(request, "users")
    body = await request.json()
    try:
        delete_user(str(body.get("username") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------- DDNS 状态与手动检查
@app.get("/api/ddns/status")
def ddns_status_route(request: Request):
    need_perm(request)
    return ddns.status()


@app.post("/api/ddns/check")
def ddns_check_route(request: Request):
    """手动立即检查 (同步路由, 线程池执行不阻塞事件循环)"""
    need_perm(request, "system")
    return ddns.check(force=True)


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    ddns.start()
    # HTTPS 监听 (证书配置后启用): 127.0.0.1:8443, 外网经 portproxy 18002 → 8443 进入;
    # 8000 保持纯 HTTP 给局域网, cookie 不带 Secure, 两协议登录态互通
    ssl_cert, ssl_key = CFG.get("ssl_cert") or "", CFG.get("ssl_key") or ""
    if ssl_cert and ssl_key and os.path.exists(ssl_cert):
        https_cfg = uvicorn.Config(app, host="127.0.0.1", port=8443,
                                   ssl_certfile=ssl_cert, ssl_keyfile=ssl_key,
                                   log_level="warning")
        threading.Thread(target=uvicorn.Server(https_cfg).run, daemon=True).start()
    elif ssl_cert:
        print("[server] ssl_cert 已配置但证书文件不存在, 8443 HTTPS 未启用", flush=True)

    # 80 端口 301 跳转: 裸输域名 (不带端口) 时, 浏览器 https-first 落在 443 失败退回
    # http://域名:80, 由这里引导到 https 主入口。家宽 80 被封时无副作用, ISP 放开后自动生效。
    _rd_domain = (CFG.get("ddns") or {}).get("domain", "")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and _rd_domain:
        _rd_port = int(CFG.get("v6_port", 18002))

        async def _redirect_app(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif msg["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
                return
            from urllib.parse import quote
            path = quote(scope.get("path", "/"), safe="/%:@")
            qs = scope.get("query_string") or b""
            loc = "https://" + _rd_domain + ":" + str(_rd_port) + path
            if qs:
                loc += "?" + qs.decode("latin1")
            body = b"301"
            await send({"type": "http.response.start", "status": 301, "headers": [
                (b"location", loc.encode("latin1")),
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})

        rd_cfg = uvicorn.Config(_redirect_app, host="::", port=80, log_level="warning")
        threading.Thread(target=uvicorn.Server(rd_cfg).run, daemon=True).start()
        print(f"[server] 80 端口 301 跳转已启用 -> https://{_rd_domain}:{_rd_port}", flush=True)
    uvicorn.run(app, host=CFG.get("listen_host", "0.0.0.0"),
                port=CFG["listen_port"], log_level="warning")
