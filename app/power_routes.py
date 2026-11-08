# -*- coding: utf-8 -*-
"""电源控制与局域网唤醒"""
import os
import re
import socket
import subprocess

from fastapi import APIRouter, HTTPException, Request

from auth import need_perm, CFG
from app_config import save_ui_keys

router = APIRouter()


@router.post("/api/power/shutdown")
async def power_shutdown(request: Request):
    need_perm(request, "system")
    subprocess.run(["shutdown", "/s", "/t", "10"],
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return {"ok": True, "delay": 10, "action": "shutdown"}


@router.post("/api/power/restart")
async def power_restart(request: Request):
    need_perm(request, "system")
    subprocess.run(["shutdown", "/r", "/t", "10"],
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return {"ok": True, "delay": 10, "action": "restart"}


@router.post("/api/power/cancel")
async def power_cancel(request: Request):
    need_perm(request, "system")
    r = subprocess.run(["shutdown", "/a"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if r.returncode != 0:
        raise HTTPException(400, "没有正在等待的关机/重启")
    return {"ok": True}


def _magic_packet(mac: str) -> bytes:
    mac_clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(mac_clean) != 12:
        raise ValueError("MAC 地址格式无效")
    return bytes.fromhex("FF" * 6 + mac_clean.lower() * 16)


@router.post("/api/wol/send")
async def wol_send(request: Request):
    need_perm(request, "system")
    body = await request.json()
    try:
        pkt = _magic_packet(body.get("mac") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(3):
            for port in (9, 7):
                s.sendto(pkt, ("255.255.255.255", port))
        return {"ok": True}
    except OSError as e:
        raise HTTPException(500, f"发送失败: {e}")
    finally:
        s.close()


@router.get("/api/wol/devices")
def wol_devices_list(request: Request):
    need_perm(request, "system")
    return {"devices": CFG.get("wol_devices", [])}


@router.post("/api/wol/devices")
async def wol_devices_save(request: Request):
    need_perm(request, "system")
    body = await request.json()
    devices = []
    seen = set()
    for d in body.get("devices") or []:
        name = str(d.get("name") or "").strip()[:30]
        mac = str(d.get("mac") or "").strip()
        try:
            _magic_packet(mac)
        except ValueError:
            raise HTTPException(400, f"MAC 地址格式无效: {mac}")
        key = mac.lower()
        if key in seen or not name:
            continue
        seen.add(key)
        devices.append({"name": name, "mac": mac})
    CFG["wol_devices"] = devices
    save_ui_keys(CFG)
    return {"ok": True, "devices": devices}
