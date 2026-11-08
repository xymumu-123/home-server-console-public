# -*- coding: utf-8 -*-
"""轻量 DDNS: 检测本机公网 IPv6, 变化时通过 DNS 服务商回调接口更新 AAAA 记录。

独立模块: 自带线程/状态/日志, 不依赖 app.py;
app 启动时调用 ddns.start() 启动后台线程, 状态经 ddns.status() 读取,
ddns.check(force=True) 可手动立即检查。"""
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
from urllib.parse import parse_qsl

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data.db")
LOG_PATH = os.path.join(BASE, "ddns.log")
LOG_KEEP_DAYS = 7

_lock = threading.Lock()
_status = {
    "enabled": None, "domain": None, "ip": None, "last_ip": None,
    "last_check": None, "last_result": "尚未运行", "last_update": None,
}


def _cfg():
    """每次检查时重读配置 (设置改动无需重启); 配置来自 config.ini"""
    try:
        import app_config
        return app_config.load().get("ddns", {})
    except Exception:
        return {}


def _kv_get(key):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _kv_set(key, value):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("INSERT INTO kv (k, v) VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _log_rotate():
    """删除 7 天前的日志行 (只读首行判断, 需要时才整体重写)"""
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            first = f.readline()
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})", first)
        if not m:
            return
        first_ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
        if first_ts >= time.time() - LOG_KEEP_DAYS * 86400:
            return                                   # 首行还在 7 天内, 无需轮转
        cutoff = time.time() - LOG_KEEP_DAYS * 86400
        keep = []
        for ln in open(LOG_PATH, encoding="utf-8"):
            m2 = re.match(r"\[(\d{4}-\d{2}-\d{2})", ln)
            if m2:
                try:
                    if time.mktime(time.strptime(m2.group(1), "%Y-%m-%d")) >= cutoff:
                        keep.append(ln)
                    continue
                except ValueError:
                    pass
            keep.append(ln)                          # 无日期前缀的行保留
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except Exception:
        pass


def _log(msg):
    _log_rotate()
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _session():
    s = requests.Session()
    s.trust_env = False          # 绕过系统坏代理 (直连)
    return s


def _get_ipv6(cfg):
    """公共 URL 服务获取公网 IPv6 (与 ddns-go 的 url 方式一致)"""
    s = _session()
    for url in cfg.get("ip_urls", []):
        try:
            ip = s.get(url, timeout=10).text.strip().strip('"').split()[0]
            ipaddress.IPv6Address(ip)      # 校验, 非法则换下一个源
            return ip
        except Exception:
            continue
    return None


def _resolved(domain):
    """当前 DNS 解析到的 IPv6 集合 (操作系统解析)"""
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(domain, None, socket.AF_INET6)}
    except Exception:
        return set()


def _update(cfg, ip, hostname=None):
    """调用西部数码回调接口更新 AAAA 记录 (照抄 ddns-go 的 callback 模板)"""
    host = hostname or cfg["domain"]
    qs = cfg["api_key"].replace("#{domain}", host).replace("#{ip}", ip)
    s = _session()
    last_err = ""
    for method in ("get", "post"):         # 回调接口 GET/POST 都试, 以实际响应为准
        try:
            if method == "get":
                r = s.get(cfg["api_url"] + "?" + qs, timeout=15)
            else:
                r = s.post(cfg["api_url"], data=dict(parse_qsl(qs)), timeout=15)
            body = (r.text or "")[:300]
            if r.status_code == 200:
                return {"method": method.upper(), "status": 200, "body": body}
            last_err = f"HTTP {r.status_code}: {body}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err or "请求失败")


def check(force=False):
    """单次检查: 获取公网 IPv6 → 对比 DNS 解析与上次记录 → 必要时更新。
    返回 {ip, changed, updated, message}"""
    with _lock:
        cfg = _cfg()
        if not cfg.get("enabled", True):
            _status.update({"enabled": False, "last_check": time.strftime("%H:%M:%S"),
                            "last_result": "已禁用"})
            return {"enabled": False, "message": "DDNS 已禁用"}
        domain = cfg.get("domain", "")
        _status["enabled"] = True
        _status["domain"] = domain

        ip = _get_ipv6(cfg)
        if not ip:
            _status["last_check"] = time.strftime("%H:%M:%S")
            _status["last_result"] = "无法获取公网 IPv6 (所有检测源失败)"
            _log(_status["last_result"])
            return {"enabled": True, "ip": None, "changed": None,
                    "updated": False, "message": _status["last_result"]}

        last_ip = _kv_get("ddns_last_ip")
        last_upd_ip = _kv_get("ddns_last_upd_ip")
        last_upd_ts = float(_kv_get("ddns_last_upd_ts") or 0)
        resolved = _resolved(domain)
        mismatch = bool(resolved) and ip not in resolved
        # DNS 记录解析有传播/缓存延迟: 同一 IP 刚更新过 (10 分钟内) 不重复打接口,
        # 否则会出现 "不一致 → 已更新 → 仍不一致" 的每分钟循环
        recently_submitted = (ip == last_upd_ip and time.time() - last_upd_ts < 600)
        need = force or ip != last_ip or (mismatch and not recently_submitted)
        reason = ("首次运行" if not last_ip
                  else "本机公网 IP 变化" if ip != last_ip
                  else "DNS 记录与公网 IP 不一致" if need
                  else "IP 未变化")

        if not need:
            _status.update({"ip": ip, "last_ip": last_ip,
                            "last_check": time.strftime("%H:%M:%S"),
                            "last_result": ("已提交更新, 等待 DNS 生效" if mismatch
                                            else "正常 (记录与公网 IP 一致)")})
            if mismatch:
                _log(f"IP 未变化 ({ip}), DNS 记录暂未同步到新值, 等待生效 (10 分钟内已提交, 不重复更新)")
            else:
                _log(f"IP 未变化 ({ip}), 记录正常")
            return {"enabled": True, "ip": ip, "changed": False,
                    "updated": False,
                    "message": _status["last_result"]}

        try:
            resp = _update(cfg, ip)
            # smb 子域 (游戏库外网 SMB 隧道用) 跟随主域同步更新, 失败不影响主域
            try:
                _update(cfg, ip, hostname="smb." + domain)
            except Exception:
                pass
        except Exception as e:
            _status["last_check"] = time.strftime("%H:%M:%S")
            _status["last_result"] = f"更新失败: {e}"
            _log(f"{reason}, 更新失败: {e}")
            return {"enabled": True, "ip": ip, "changed": True,
                    "updated": False, "message": f"更新失败: {e}"}

        _kv_set("ddns_last_ip", ip)
        _kv_set("ddns_last_upd_ip", ip)
        _kv_set("ddns_last_upd_ts", time.time())
        _kv_set("ddns_last_check", time.strftime("%Y-%m-%d %H:%M:%S"))
        _kv_set("ddns_last_result", f"{resp['method']} {resp['status']} {resp['body']}")
        _status.update({"ip": ip, "last_ip": ip,
                        "last_check": time.strftime("%H:%M:%S"),
                        "last_result": f"已更新 ({resp['method']} {resp['status']})",
                        "last_update": time.strftime("%Y-%m-%d %H:%M:%S")})
        _log(f"{reason}, 已更新 {domain} AAAA → {ip} ({resp['method']} {resp['status']})")
        return {"enabled": True, "ip": ip, "changed": True,
                "updated": True, "message": f"{reason}, 已更新 → {ip}"}


def _log_tail(n=30):
    """ddns.log 最后 n 行 (供前端详情面板展示)"""
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []


def _log_view(lines):
    """日志展示过滤: 变化记录全保留, '未变化'类记录只保留最新一条"""
    marked = [(i, ln, "未变化" in ln) for i, ln in enumerate(lines)]
    unchanged = [m for m in marked if m[2]]
    keep_idx = unchanged[-1][0] if unchanged else None
    return [ln for i, ln, u in marked if (not u) or i == keep_idx]


def status():
    s = dict(_status)
    cfg = _cfg()
    s["interval_min"] = cfg.get("interval_min", 5)
    if s.get("ip"):
        s["consistent"] = s["ip"] in _resolved(cfg.get("domain", ""))
    else:
        s["consistent"] = None
    raw = _log_tail(400)                     # 覆盖约 7 天的量, 供过滤
    s["log_tail"] = _log_view(raw)[-40:]
    return s


def _worker():
    time.sleep(3)                          # 等服务初始化
    while True:
        try:
            check()
        except Exception as e:
            _log(f"检查异常: {e}")
        time.sleep(_cfg().get("interval_min", 5) * 60)


def start():
    threading.Thread(target=_worker, daemon=True).start()
