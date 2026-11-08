# -*- coding: utf-8 -*-
"""下载机: aria2 进程管理 + RPC 代理 + 任务 CRUD"""
import os
import subprocess
import time

import requests
from fastapi import APIRouter, HTTPException, Request

from auth import need_perm, CFG

router = APIRouter()

_aria2_last_check = 0.0
_trackers_last = 0.0
_aria2_trackers_applied = False
_TRACKER_SOURCES = [
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://ghproxy.net/https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
]
TRACKER_REFRESH = 6 * 3600


def _trackers_cache_path():
    return os.path.join(os.path.dirname(CFG.get("aria2_bin", "")) or ".", "trackers.txt")


def _apply_trackers(trackers: str):
    aria2_rpc("changeGlobalOption", [{"bt-tracker": trackers}])


def update_trackers(force=False):
    """拉取公共 tracker 列表注入 aria2 (提升磁力解析成功率); 失败时回退本地缓存"""
    global _trackers_last, _aria2_trackers_applied
    if not force and time.time() - _trackers_last < TRACKER_REFRESH:
        return
    if not aria2_ready():
        return
    _trackers_last = time.time()
    trackers = ""
    sess = requests.Session()
    sess.trust_env = False        # 绕过系统坏代理 (直连)
    for url in _TRACKER_SOURCES:
        try:
            r = sess.get(url, timeout=15)
            r.raise_for_status()
            lines = [x.strip() for x in r.text.splitlines()
                     if x.strip() and not x.startswith(("#", "//"))]
            if len(lines) >= 5:
                trackers = ",".join(lines[:150])
                try:
                    with open(_trackers_cache_path(), "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
                except OSError:
                    pass
                break
        except Exception:
            continue
    if not trackers:
        _trackers_last = time.time() - TRACKER_REFRESH + 600
        try:
            with open(_trackers_cache_path(), encoding="utf-8") as f:
                lines = [x.strip() for x in f if x.strip()]
            trackers = ",".join(lines[:150])
        except OSError:
            return
    try:
        _apply_trackers(trackers)
        _aria2_trackers_applied = True
        print(f"[aria2] tracker 列表已更新 ({len(trackers.split(','))} 条)", flush=True)
    except Exception as e:
        print(f"[aria2] tracker 注入失败: {e}", flush=True)


def aria2_maintenance():
    """采样线程每 30 秒调用: 保证 aria2 存活 + tracker 注入/刷新"""
    global _aria2_last_check, _aria2_trackers_applied
    if time.time() - _aria2_last_check < 30:
        return
    bin_path = CFG.get("aria2_bin", "")
    if not bin_path or not os.path.exists(bin_path):
        return
    _aria2_last_check = time.time()
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    port_busy = s.connect_ex(("127.0.0.1", int(CFG.get("aria2_port", 6800)))) == 0
    s.close()

    if not port_busy:
        _aria2_trackers_applied = False
        try:
            os.makedirs(CFG.get("download_dir", ""), exist_ok=True)
            session_file = os.path.join(os.path.dirname(bin_path), "session.txt")
            try:
                open(session_file, "a", encoding="utf-8").close()
            except OSError:
                session_file = ""
            args = [bin_path, "--enable-rpc",
                    "--rpc-listen-port", str(CFG.get("aria2_port", 6800)),
                    "--rpc-secret", CFG.get("aria2_secret", ""),
                    "--dir", CFG.get("download_dir", ""),
                    "--continue", "--max-concurrent-downloads=3", "--seed-time=0",
                    "--file-allocation=none", "--quiet=true"]
            if session_file:
                args += ["--save-session", session_file, "--input-file", session_file,
                         "--force-save=true", "--auto-save-interval=30"]
            subprocess.Popen(args,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as e:
            print(f"[aria2] 启动失败: {e}", flush=True)
        return

    if not _aria2_trackers_applied:
        try:
            with open(_trackers_cache_path(), encoding="utf-8") as f:
                lines = [x.strip() for x in f if x.strip()]
            if lines:
                _apply_trackers(",".join(lines[:150]))
            _aria2_trackers_applied = True
        except Exception:
            pass
    if time.time() - _trackers_last > TRACKER_REFRESH:
        update_trackers(force=True)


def aria2_rpc(method, params=None):
    url = f"http://127.0.0.1:{int(CFG.get('aria2_port', 6800))}/jsonrpc"
    payload = {"jsonrpc": "2.0", "id": "mon", "method": "aria2." + method,
               "params": ["token:" + CFG.get("aria2_secret", "")] + (params or [])}
    r = requests.post(url, json=payload, timeout=5)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "rpc error"))
    return data.get("result")


def aria2_ready() -> bool:
    bin_path = CFG.get("aria2_bin", "")
    return bool(bin_path) and os.path.exists(bin_path)


DL_KEYS = ["gid", "totalLength", "completedLength", "downloadSpeed", "status", "files", "bittorrent", "errorMessage"]


@router.get("/api/dl/summary")
def dl_summary(request: Request):
    need_perm(request, "downloads")
    if not aria2_ready():
        return {"installed": False}
    try:
        act = aria2_rpc("tellActive", [["downloadSpeed"]]) or []
        return {"installed": True, "active": len(act),
                "speed": sum(int(a.get("downloadSpeed", 0)) for a in act)}
    except Exception as e:
        return {"installed": True, "error": str(e)}


@router.get("/api/dl/list")
def dl_list(request: Request):
    need_perm(request, "downloads")
    if not aria2_ready():
        raise HTTPException(503, "下载机未安装")
    out = []
    for st, params in (("tellActive", [DL_KEYS]),
                       ("tellWaiting", [0, 20, DL_KEYS]),
                       ("tellStopped", [0, 20, DL_KEYS])):
        try:
            items = aria2_rpc(st, params) or []
        except (requests.RequestException, RuntimeError) as e:
            raise HTTPException(503, f"aria2 未运行: {e}")
        for it in items:
            files = it.get("files") or []
            path = files[0].get("path", "") if files else ""
            name = ((it.get("bittorrent") or {}).get("info", {}).get("name")
                    or (os.path.basename(path) if path else it["gid"]))
            out.append({"gid": it["gid"], "name": name, "status": it["status"],
                        "total": int(float(it.get("totalLength", 0))),
                        "done": int(float(it.get("completedLength", 0))),
                        "speed": int(it.get("downloadSpeed", 0)),
                        "path": path,
                        "error": it.get("errorMessage", "")})
    return {"items": out, "dir": CFG.get("download_dir", "")}


@router.post("/api/dl/add")
async def dl_add(request: Request):
    need_perm(request, "downloads")
    if not aria2_ready():
        raise HTTPException(503, "下载机未安装")
    body = await request.json()
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "链接不能为空")
    directory = os.path.normpath(str(body.get("dir") or CFG.get("download_dir", "")))
    if not os.path.isdir(directory):
        raise HTTPException(400, f"目标目录不存在: {directory}")
    base = os.path.normpath(CFG.get("download_dir", ""))
    if not base or not directory.lower().startswith(base.lower() + os.sep):
        raise HTTPException(400, "目标目录必须在下载目录内")
    try:
        gid = aria2_rpc("addUri", [[url], {"dir": directory}])
    except Exception as e:
        raise HTTPException(503, f"添加失败: {e}")
    return {"gid": gid}


@router.post("/api/dl/remove")
async def dl_remove(request: Request):
    need_perm(request, "downloads")
    body = await request.json()
    gid = body.get("gid") or ""
    try:
        aria2_rpc("remove", [gid])
    except Exception as e1:
        try:
            aria2_rpc("removeDownloadResult", [gid])
        except Exception as e2:
            raise HTTPException(404, f"移除失败: {e1} / {e2}")
    return {"ok": True}


@router.post("/api/dl/pause")
async def dl_pause(request: Request):
    need_perm(request, "downloads")
    body = await request.json()
    try:
        aria2_rpc("pause", [body.get("gid") or ""])
    except Exception as e:
        raise HTTPException(400, f"暂停失败: {e}")
    return {"ok": True}


@router.post("/api/dl/resume")
async def dl_resume(request: Request):
    need_perm(request, "downloads")
    body = await request.json()
    try:
        aria2_rpc("unpause", [body.get("gid") or ""])
    except Exception as e:
        raise HTTPException(400, f"继续失败: {e}")
    return {"ok": True}


@router.post("/api/dl/clear")
async def dl_clear(request: Request):
    """清空已完成/出错/已移除的任务记录"""
    need_perm(request, "downloads")
    n = 0
    try:
        stopped = aria2_rpc("tellStopped", [0, 200, ["gid", "status"]]) or []
        for it in stopped:
            if it.get("status") in ("complete", "error", "removed"):
                try:
                    aria2_rpc("removeDownloadResult", [it["gid"]])
                    n += 1
                except Exception:
                    pass
    except (requests.RequestException, RuntimeError) as e:
        raise HTTPException(503, f"aria2 未运行: {e}")
    return {"ok": True, "removed": n}
