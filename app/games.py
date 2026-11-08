# -*- coding: utf-8 -*-
"""游戏库: 人工策展模式 — 扫描只发现, 人工添加配置后才上架。

设计(与用户对齐):
- 发现: 扫所有 game_dirs 的一级目录, 只出"待添加"清单, 不写注册表
- 上架: 手动配置 显示名/封面/启动主程序/存档路径模板 (exe 与存档路径必填),
  存档模板支持 {user}/{pc} 变量, 由启动器在各电脑展开, 首次运行免手输
- 启动: nasgame:// → launcher.ps1 → /launch 租约 → 拉存档 → 启动 → 回传
- 删除: "删除显示"=移出库+清存档快照; "卸载游戏"=再删游戏本体目录 (双重守卫)
- 客户端: setup.bat 注册 nasgame:// 协议 (HKCU 免管理员), launcher.ps1 自更新
"""
import ctypes
import hmac
import json
import os
import re
import shutil
import subprocess
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app_config import ConfigError
from auth import CFG, is_authed, need_perm

router = APIRouter(prefix="/api/games")

BASE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(BASE, "static", "game_icons")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

JUNK_EXE = re.compile(r"unitycrash|crashhandler|crashreport|uninstall|setup|"
                      r"redist|dxsetup|vcredist|dotnet|directx|installer|"
                      r"updater|patcher|launcherset", re.I)
JUNK_DIR = re.compile(r"redist|directx|vcredist|dotnet|__macosx|crashreport", re.I)

COVER_EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "webp": "image/webp"}
COVER_MAGIC = ((b"\x89PNG", "png"), (b"\xff\xd8", "jpg"), (b"RIFF", "webp"))
COVER_MAX = int(CFG.get("cover_max_mb", 8)) * 1024 * 1024


# ---------------------------------------------------------------- 基础
def _games_roots():
    """游戏库扫描根列表: 设置页保存的 game_dirs (列表), 兼容旧的 games_root (单目录)"""
    dirs = [str(d) for d in (CFG.get("game_dirs") or []) if str(d).strip()]
    if dirs:
        return dirs
    return [CFG["games_root"]] if CFG.get("games_root") else []


def _saves_root():
    return CFG.get("saves_root", "")


def _reg_path():
    # 注册表锚定在 saves_root 同级 (G:\movenas\games.json), 不随扫描根增删而移动
    saves = _saves_root()
    if saves:
        return os.path.join(os.path.dirname(saves), "games.json")
    roots = _games_roots()
    if roots:
        return os.path.join(os.path.dirname(roots[0]), "games.json")
    return None


def _to_unc(path):
    """服务器本地路径 → 客户端可访问的 UNC (按 games_unc_map 前缀替换, 长前缀优先)"""
    m = {str(k): str(v) for k, v in (CFG.get("games_unc_map") or {}).items()}
    legacy_root, legacy_unc = CFG.get("games_root"), CFG.get("games_unc_base")
    if legacy_root and legacy_unc and legacy_root not in m:
        m[legacy_root] = legacy_unc
    best_l, best_u = "", ""
    for l, u in m.items():
        l = os.path.normpath(l)
        if path.lower().startswith(l.lower() + "\\") and len(l) > len(best_l):
            best_l, best_u = l, u.rstrip("\\")
    if best_l:
        # 防御: UNC 前缀开头统一为两个反斜杠 (手改配置多打了也不出错)
        best_u = "\\\\" + best_u.lstrip("\\")
        return best_u + path[len(best_l):]
    return path


def _folder_in_roots(folder):
    """目录必须位于某个扫描根下 (config/卸载的路径守卫)"""
    if not folder:
        return False
    fn = os.path.normpath(folder).lower().rstrip("\\") + "\\"
    return any(fn.startswith(os.path.normpath(r).lower().rstrip("\\") + "\\")
               for r in _games_roots())


def _registry_unavailable():
    """注册表所在盘/目录不可用 (如存档盘被移除) 时返回明确错误文案, 否则 None"""
    p = _reg_path()
    if not p:
        return "未配置游戏存档根目录 (config.ini [paths] saves_root), 无法定位游戏注册表"
    parent_dir = os.path.dirname(p)
    if not os.path.isdir(parent_dir):
        return f"游戏注册表所在目录不存在: {parent_dir} (请检查 config.ini 的 saves_root 指向的盘是否在线)"
    return None


def _registry():
    p = _reg_path()
    if p and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"games": {}, "leases": {}, "updated": 0}


def _save_registry(reg):
    p = _reg_path()
    if not p:
        return
    reg["updated"] = time.time()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        raise ConfigError(f"游戏注册表写入失败: {p} ({e}) — 请检查该盘是否在线") from e


def _token_ok(request: Request) -> bool:
    return hmac.compare_digest(request.headers.get("X-Game-Token", ""),
                               CFG.get("games_token", "-"))


def _token_ok_from_query(token, request=None) -> bool:
    # request 参数仅为兼容既有调用点 (save/pull|push|get|list, client/ps1|bat 均传 2 参)
    return bool(token) and hmac.compare_digest(token, CFG.get("games_token", "-"))


def _client_auth(request: Request):
    if not _token_ok(request):
        raise HTTPException(401, "invalid game token")


# ---------------------------------------------------------------- 启发式发现
def _file_description(path):
    """读取 exe 的 FileDescription 版本信息 (ctypes, 无第三方依赖)"""
    try:
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, data):
            return None
        ptr = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not ver.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                  ctypes.byref(ptr), ctypes.byref(ln)) or ln.value < 4:
            return None
        words = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16 * (ln.value // 2))).contents
        blocks = ["%04x%04x" % (words[i], words[i + 1])
                  for i in range(0, ln.value // 2, 2)]
        blocks += ["040904b0", "040904e4"]
        for b in blocks:
            if ver.VerQueryValueW(data, "\\StringFileInfo\\" + b + "\\FileDescription",
                                  ctypes.byref(ptr), ctypes.byref(ln)) and ln.value > 1:
                s = ctypes.wstring_at(ptr.value, ln.value - 1).strip()
                if s:
                    return s
        return None
    except Exception:
        return None


def _collect_exes(folder, depth=0, out=None):
    if out is None:
        out = []
    if depth > 6:
        return out
    try:
        entries = os.listdir(folder)
    except OSError:
        return out
    for name in entries:
        full = os.path.join(folder, name)
        if os.path.isdir(full):
            if not JUNK_DIR.search(name):
                _collect_exes(full, depth + 1, out)
        elif name.lower().endswith(".exe") and not JUNK_EXE.search(name):
            try:
                out.append({"path": full, "size": os.path.getsize(full)})
            except OSError:
                pass
    return out


def _score_exe(exe, folder_name):
    """候选启动 exe 排序: 文件大小为主信号 (游戏主程序几乎总是最大的),
    辅以描述信息与浅层加分。目录名匹配不加分 (ID 编号目录无意义)。
    描述含安装器/运行库关键词 → 大幅降分 (大文件但不是游戏)"""
    score = exe["size"] / (1024 * 1024)            # 主排序: 文件大小 (MB), 1MB = 1 分
    desc = exe.get("desc")
    if desc:
        score += 15                                 # 有版本信息 = 正式程序
        desc_low = desc.lower()
        if any(kw in desc_low for kw in (
                'setup', 'install', 'redistribut', 'framework',
                'visual c++', 'physx', 'directx', 'runtime', 'package launcher')):
            score -= 80                             # 安装器/运行库: 体积大但非游戏
    stem = os.path.splitext(os.path.basename(exe["path"]))[0].lower()
    if stem == folder_name.lower():
        score += 20                                 # 与目录同名仍是有用信号 (但不主导)
    if exe["path"].count(os.sep) - folder_name.count(os.sep) <= 1:
        score += 10                                 # 浅层优先
    return score


def _exe_candidates(folder, limit=None):
    """某游戏目录下的启动 exe 候选 (含描述/大小, 按文件大小降序 = 越大越可能是主程序)"""
    exes = _collect_exes(folder)
    folder_name = os.path.basename(os.path.normpath(folder))
    for e in exes:
        e["desc"] = _file_description(e["path"])
        e["score"] = _score_exe(e, folder_name)
    exes.sort(key=lambda x: -x["score"])
    out = [{"path": e["path"], "desc": e.get("desc"), "score": round(e["score"]),
            "size_mb": round(e["size"] / (1024 * 1024), 1)}
           for e in exes]
    return out[:limit] if limit else out


def _icon_url(gid):
    """封面优先 (人工上传), 其次 exe 提取的自动图标"""
    if os.path.isdir(ICON_DIR):
        for ext in COVER_EXTS:
            p = os.path.join(ICON_DIR, f"{gid}_cover.{ext}")
            if os.path.exists(p):
                return f"/static/game_icons/{gid}_cover.{ext}"
        if os.path.exists(os.path.join(ICON_DIR, gid + ".png")):
            return "/static/game_icons/" + gid + ".png"
    return None


def scan_discovered():
    """纯发现: 各扫描根下的一级目录 (跳过 . 开头与已在库中的), 不写注册表"""
    reg = _registry()
    known = set(reg.get("games", {}))
    out = []
    for root in [r for r in _games_roots() if os.path.isdir(r)]:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or name in known:
                continue
            fp = os.path.join(root, name)
            if not os.path.isdir(fp):
                continue
            cands = _exe_candidates(fp, limit=5)
            out.append({"id": name, "folder": fp,
                        "name": (next((c["desc"] for c in cands if c.get("desc")), None)
                                 or name),
                        "candidates": cands})
    return out


def _extract_icons(exe_by_id):
    """批量提取 exe 关联图标为 PNG (PowerShell System.Drawing), 作默认封面"""
    if not exe_by_id:
        return
    os.makedirs(ICON_DIR, exist_ok=True)
    lines = ["Add-Type -AssemblyName System.Drawing"]
    for gid, exe in exe_by_id.items():
        out = os.path.join(ICON_DIR, gid + ".png")
        lines.append(
            f"try {{ $i = [System.Drawing.Icon]::ExtractAssociatedIcon('{exe}'); "
            f"$i.ToBitmap().Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png) "
            f"}} catch {{ }}")
    ps = os.path.join(ICON_DIR, "_icons.ps1")
    with open(ps, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", ps], creationflags=CREATE_NO_WINDOW,
                   timeout=120, capture_output=True)
    try:
        os.remove(ps)
    except OSError:
        pass


# ---------------------------------------------------------------- 租约
LEASE_EXPIRE = int(CFG.get("lease_hours", 6)) * 3600


def _lease_check(reg, gid, pc):
    lease = reg.get("leases", {}).get(gid)
    if lease and lease.get("pc") != pc and time.time() - lease.get("started", 0) < LEASE_EXPIRE:
        return lease
    return None


# ---------------------------------------------------------------- 存档快照
def _push_snapshot(gid, pc, data: bytes):
    root = os.path.join(_saves_root(), gid)
    os.makedirs(root, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"{pc}-{ts}.zip"
    with open(os.path.join(root, name), "wb") as f:
        f.write(data)
    # 每个游戏保留最近 12 份快照 (清单实时读目录, 不再写注册表)
    snaps = sorted(f for f in os.listdir(root) if f.endswith(".zip"))
    for old in snaps[:-12]:
        try:
            os.remove(os.path.join(root, old))
        except OSError:
            pass
    return name


def _snapshots(gid):
    if not _saves_root():
        return []                       # saves_root 未配置: 视为无快照
    root = os.path.join(_saves_root(), gid)
    out = []
    if os.path.isdir(root):
        for f in sorted(os.listdir(root)):
            if f.endswith(".zip"):
                full = os.path.join(root, f)
                out.append({"file": f, "pc": f.split("-")[0],
                            "ts": os.path.getmtime(full),
                            "size": os.path.getsize(full)})
    return sorted(out, key=lambda x: -x["ts"])


# ---------------------------------------------------------------- 校验
def _check_save_tpl(tpl, folder):
    """存档路径模板校验: 盘符绝对路径 + 变量合法 + 不得位于游戏目录内。
    变量支持 Windows 原生 %VAR% (客户端展开, 如 %USERPROFILE%) 与旧式 {user}/{pc}"""
    tpl = (tpl or "").strip()
    if not tpl:
        raise HTTPException(400, "存档路径不能为空")
    if len(tpl) > 260:
        raise HTTPException(400, "存档路径过长")
    # 两种合法开头: 盘符绝对路径, 或以原生变量开头 (%USERPROFILE%\...)
    if not re.match(r"^[A-Za-z]:\\", tpl) and not re.match(r"^%[A-Za-z0-9_]+%\\", tpl):
        raise HTTPException(400, "存档路径须为盘符或原生变量 (%USERPROFILE% 等) 开头")
    # 剥离合法的 %VAR% 变量对后, 不允许再有裸 % (防半开/乱写)
    if "%" in re.sub(r"%[A-Za-z0-9_]+%", "", tpl):
        raise HTTPException(400, "百分号用法无效 (原生变量应成对写作 %USERPROFILE%)")
    for part in re.split(r"[{}]", tpl)[1::2]:
        if part not in ("user", "pc"):
            raise HTTPException(400, f"不支持的变量 {{{part}}} (建议改用原生变量如 %USERPROFILE%)")
    expanded = re.sub(r"%[A-Za-z0-9_]+%", "_env_", tpl)
    expanded = expanded.replace("{user}", "_u_").replace("{pc}", "_p_")
    if folder and os.path.normpath(expanded).lower().startswith(
            os.path.normpath(folder).lower().rstrip("\\") + "\\"):
        raise HTTPException(400, "存档路径不能设在游戏目录内")
    return tpl


# ---------------------------------------------------------------- API (页面用)
@router.get("/list")
def games_list(request: Request):
    if not is_authed(request):
        raise HTTPException(401, "unauth")
    reg = _registry()
    games = []
    need_icons = {}
    for gid, g in sorted(reg.get("games", {}).items()):
        folder = g.get("folder", "")
        exe = g.get("exe") or ""
        games.append({"id": gid, "name": g.get("name") or gid,
                      "folder": folder, "exe": exe,
                      "save_tpl": g.get("save_tpl", ""),
                      "configured": bool(g.get("save_tpl")),
                      "folder_missing": bool(folder) and not os.path.isdir(folder),
                      "icon": _icon_url(gid),
                      "last_played": g.get("last_played"),
                      "added_ts": g.get("added_ts"),
                      "lease": reg.get("leases", {}).get(gid),
                      "snapshots": _snapshots(gid)[:5]})
        if exe and os.path.exists(exe) and not _icon_url(gid):
            need_icons[gid] = exe                    # 无封面无图标时补自动图标
    if need_icons:
        _extract_icons(need_icons)
    return {"games": games, "discovered": scan_discovered()}


@router.post("/config")
async def games_config(request: Request):
    """添加或更新游戏 (添加=首次保存, exe 与存档路径模板必填)"""
    need_perm(request, "games")
    body = await request.json()
    folder = str(body.get("folder") or "").strip()
    gid = str(body.get("game") or "").strip()
    name = str(body.get("name") or "").strip()
    exe = str(body.get("exe") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "游戏目录不存在")
    if not _folder_in_roots(folder):
        raise HTTPException(400, "游戏目录不在任何扫描根下")
    reg = _registry()
    if not gid:
        gid = os.path.basename(os.path.normpath(folder))
    g = reg["games"].get(gid)
    if g and g.get("folder", "").lower() != os.path.normpath(folder).lower():
        raise HTTPException(400, f"游戏ID [{gid}] 已被其他目录占用")
    if not exe or not os.path.isfile(exe):
        raise HTTPException(400, "须选择有效的启动主程序")
    if os.path.normpath(exe).lower().find(os.path.normpath(folder).lower() + "\\") != 0:
        raise HTTPException(400, "启动主程序必须位于游戏目录内")
    unavail = _registry_unavailable()
    if unavail:
        raise HTTPException(503, unavail)
    save_tpl = _check_save_tpl(body.get("save_tpl"), folder)
    g = reg["games"].setdefault(gid, {"id": gid})
    g.update({"folder": os.path.normpath(folder), "exe": os.path.normpath(exe),
              "name": name or gid, "save_tpl": save_tpl,
              "added_ts": g.get("added_ts") or time.time()})
    _save_registry(reg)
    if not _icon_url(gid):
        _extract_icons({gid: g["exe"]})
    return {"ok": True, "game": gid}


@router.post("/cover")
async def games_cover(request: Request, game: str = ""):
    """上传游戏封面 (原始字节流, png/jpg/webp, ≤8MB), 存为 <gid>_cover.<ext>"""
    need_perm(request, "games")
    gid = re.sub(r"[^0-9A-Za-z_-]", "", game or "")
    if not gid or gid not in _registry()["games"]:
        raise HTTPException(400, "游戏不存在")
    data = await request.body()
    if not data:
        raise HTTPException(400, "未收到封面文件")
    if len(data) > COVER_MAX:
        raise HTTPException(400, "封面超过 8MB")
    kind = next((k for magic, k in COVER_MAGIC if data[:len(magic)] == magic), None)
    if not kind:
        raise HTTPException(400, "仅支持 png/jpg/webp")
    os.makedirs(ICON_DIR, exist_ok=True)
    # 清掉旧封面 (换扩展名时), 自动图标 <gid>.png 不动 (作回退)
    for ext in COVER_EXTS:
        old = os.path.join(ICON_DIR, f"{gid}_cover.{ext}")
        if ext != kind and os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
    with open(os.path.join(ICON_DIR, f"{gid}_cover.{kind}"), "wb") as f:
        f.write(data)
    return {"ok": True, "icon": _icon_url(gid)}


@router.get("/exes")
def games_exes(request: Request, folder: str = ""):
    """配置弹窗的启动主程序候选列表"""
    need_perm(request, "games")
    if not folder or not os.path.isdir(folder) or not _folder_in_roots(folder):
        raise HTTPException(400, "目录无效")
    return {"candidates": _exe_candidates(folder)}


@router.post("/delete")
async def games_delete(request: Request):
    """删除游戏: mode=display 移出库并清存档快照; mode=uninstall 再卸载游戏本体"""
    need_perm(request, "games")
    body = await request.json()
    gid, mode = body.get("game") or "", body.get("mode") or ""
    if mode not in ("display", "uninstall"):
        raise HTTPException(400, "mode 须为 display 或 uninstall")
    reg = _registry()
    g = reg["games"].get(gid)
    if not g:
        raise HTTPException(404, "游戏不存在")
    folder = g.get("folder", "")
    if mode == "uninstall":
        lease = reg.get("leases", {}).get(gid)
        if lease and time.time() - lease.get("started", 0) < LEASE_EXPIRE:
            raise HTTPException(400, "游戏租约未过期, 可能仍在运行, 先等租约失效或用删除显示")
        if not _folder_in_roots(folder):
            raise HTTPException(400, "游戏目录不在扫描根下, 拒绝删除")
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    # 公共清理: 注册表条目 + 存档快照 + 封面/图标 + 租约
    unavail = _registry_unavailable()
    if unavail:
        raise HTTPException(503, unavail)
    reg["games"].pop(gid, None)
    reg.get("leases", {}).pop(gid, None)
    _save_registry(reg)
    if _saves_root():
        saves = os.path.join(_saves_root(), gid)
        if os.path.isdir(saves):
            shutil.rmtree(saves, ignore_errors=True)
    if os.path.isdir(ICON_DIR):
        for f in os.listdir(ICON_DIR):
            if f == gid + ".png" or f.startswith(gid + "_cover."):
                try:
                    os.remove(os.path.join(ICON_DIR, f))
                except OSError:
                    pass
    return {"ok": True, "mode": mode}


@router.get("/save/get-page")
def save_get_page(request: Request, game: str = "", file: str = ""):
    """页面用的快照下载 (登录即可)"""
    if not is_authed(request):
        raise HTTPException(401, "unauth")
    return save_get(request, game=game, file=file, token=CFG.get("games_token", ""))


# ---------------------------------------------------------------- API (客户端用, 令牌鉴权)
@router.post("/launch")
async def games_launch(request: Request):
    _client_auth(request)
    body = await request.json()
    gid, pc = body.get("game") or "", body.get("pc") or ""
    force = bool(body.get("force"))
    if not gid or not pc:
        raise HTTPException(400, "bad request")
    reg = _registry()
    g = reg["games"].get(gid)
    if not g or not g.get("exe"):
        raise HTTPException(404, "游戏不存在或未配置启动主程序")
    if not g.get("save_tpl"):
        raise HTTPException(400, "游戏未配置存档路径, 请先在游戏库完成配置")
    conflict = None if force else _lease_check(reg, gid, pc)
    if conflict:
        return {"conflict": True, "holder": conflict.get("pc"),
                "since": time.strftime("%H:%M", time.localtime(conflict["started"]))}
    reg.setdefault("leases", {})[gid] = {"pc": pc, "started": time.time()}
    g["last_played"] = {"pc": pc, "ts": time.time()}
    _save_registry(reg)
    return {"conflict": False, "exe": _to_unc(g["exe"]), "game": gid,
            "name": g.get("name") or gid, "save_tpl": g["save_tpl"],
            "has_snapshot": bool(_snapshots(gid))}


@router.post("/launch/end")
async def games_launch_end(request: Request):
    _client_auth(request)
    body = await request.json()
    reg = _registry()
    lease = reg.get("leases", {}).get(body.get("game") or "")
    if lease and lease.get("pc") == body.get("pc"):
        reg["leases"].pop(body.get("game"), None)
        _save_registry(reg)
    return {"ok": True}


@router.post("/save/dir")
async def save_dir_register(request: Request):
    """客户端登记本机某游戏的实际存档目录 (学习值, 仅记录)"""
    _client_auth(request)
    body = await request.json()
    gid, pc, d = body.get("game") or "", body.get("pc") or "", body.get("dir") or ""
    # d 是客户端本机路径, 服务器上不存在是正常的, 只做格式校验 (盘符开头的绝对路径)
    if not gid or not pc or not re.match(r"^[A-Za-z]:\\.+", d) or len(d) > 260:
        raise HTTPException(400, "路径格式无效")
    reg = _registry()
    g = reg["games"].get(gid)
    if not g:
        raise HTTPException(404, "游戏不在库中")
    g.setdefault("save_dirs", {})[pc] = d
    _save_registry(reg)
    return {"ok": True}


@router.post("/save/push")
async def save_push(request: Request, game: str = "", pc: str = "", token: str = ""):
    """客户端上传存档快照 zip (原始字节流)"""
    if not _token_ok_from_query(token, request):
        raise HTTPException(401, "invalid game token")
    gid = re.sub(r"[^0-9A-Za-z_-]", "", request.query_params.get("game") or "")
    pc = re.sub(r"[^0-9A-Za-z_-]", "", request.query_params.get("pc") or "pc")
    if not gid:
        raise HTTPException(400, "bad request")
    if gid not in _registry()["games"]:
        raise HTTPException(404, "游戏不在库中")
    if not _saves_root():
        raise HTTPException(503, "未配置游戏存档根目录 (config.ini [games] saves_root)")          # 只收库内游戏的快照
    data = await request.body()
    if len(data) < 4 or data[:2] != b"PK":
        raise HTTPException(400, "不是有效的 zip")
    name = _push_snapshot(gid, pc, data)
    return {"ok": True, "file": name}


@router.get("/save/pull")
def save_pull(request: Request, game: str = "", token: str = ""):
    if not _token_ok_from_query(token, request):
        raise HTTPException(401, "invalid game token")
    if not _saves_root():
        raise HTTPException(503, "未配置游戏存档根目录 (config.ini [games] saves_root)")
    root = os.path.join(_saves_root(), re.sub(r"[^0-9A-Za-z_-]", "", game))
    snaps = sorted((f for f in os.listdir(root) if f.endswith(".zip")),
                   key=lambda f: os.path.getmtime(os.path.join(root, f))) if os.path.isdir(root) else []
    if not snaps:
        raise HTTPException(404, "没有快照")
    newest = snaps[-1]
    return FileResponse(os.path.join(root, newest), media_type="application/zip",
                        filename=newest)


@router.get("/save/list")
def save_list(request: Request, game: str = "", token: str = ""):
    if not _token_ok_from_query(token, request):
        raise HTTPException(401, "invalid game token")
    return {"snapshots": _snapshots(re.sub(r"[^0-9A-Za-z_-]", "", game))[-10:]}


@router.get("/save/get")
def save_get(request: Request, game: str = "", file: str = "", token: str = ""):
    if not _token_ok_from_query(token, request):
        raise HTTPException(401, "invalid game token")
    # game 严格消毒: 绝对路径会覆盖 os.path.join 的前缀, 不消毒即任意文件读取
    if not _saves_root():
        raise HTTPException(503, "未配置游戏存档根目录 (config.ini [games] saves_root)")
    root = os.path.join(_saves_root(), re.sub(r"[^0-9A-Za-z_-]", "", game))
    f = os.path.abspath(os.path.join(root, os.path.basename(file)))
    if not f.startswith(os.path.abspath(root) + os.sep) or not os.path.isfile(f):
        raise HTTPException(404, "快照不存在")
    return FileResponse(f, media_type="application/zip", filename=os.path.basename(f))


# ---------------------------------------------------------------- 客户端生成
def _load_template(name, server, token):
    tpl = open(os.path.join(BASE, name), encoding="utf-8").read()
    # 服务器地址用配置的通用地址 (内外网都可达), 不用 request.base_url
    server = CFG.get("games_server_url") or server
    return tpl.replace("__SERVER__", server).replace("__TOKEN__", token)


@router.get("/client/ps1")
def client_ps1(request: Request, token: str = ""):
    if not is_authed(request) and not _token_ok_from_query(token, request):
        raise HTTPException(401, "unauth")
    server = str(request.base_url).rstrip("/")
    # UTF-8 + BOM: PowerShell 5.1 靠 BOM 识别编码, 否则按 ANSI 读会乱码
    # 文本模式读模板已把换行归一成 \n, 这里统一转回 CRLF (Windows 客户端)
    tpl = _load_template(
        "launcher_template.ps1", server, CFG.get("games_token", "")).replace("\n", "\r\n")
    body = b"\xef\xbb\xbf" + tpl.encode("utf-8")
    return Response(body, media_type="text/plain; charset=utf-8")


@router.get("/client/smbcred")
def client_smbcred(request: Request, token: str = ""):
    """下发 SMB 专用账户凭据 (客户端启动器自动 cmdkey 用; 低权限只读账户)。
    启动器经请求头带令牌 (X-Game-Token), 浏览器经查询参数 (token=)"""
    if not _token_ok(request) and not is_authed(request) and not _token_ok_from_query(token, request):
        raise HTTPException(401, "unauth")
    if not CFG.get("smb_user") or not CFG.get("smb_pass"):
        raise HTTPException(404, "smb 账户未配置")
    domain = (CFG.get("ddns") or {}).get("domain", "")
    if not domain:
        raise HTTPException(404, "ddns 域名未配置")
    return {"user": CFG["smb_user"], "pass": CFG["smb_pass"], "target": "smb." + domain}


@router.get("/client/bat")
def client_bat(request: Request, token: str = ""):
    if not is_authed(request) and not _token_ok_from_query(token, request):
        raise HTTPException(401, "unauth")
    server = str(request.base_url).rstrip("/")
    # bat 必须 GBK + CRLF: LF 换行会让 cmd 解析错位 (从行中间撕裂), 中文用 GBK 码表
    tpl = _load_template("setup_template.bat", server, CFG.get("games_token", ""))
    tpl = tpl.replace("\r\n", "\n").replace("\n", "\r\n")
    return Response(tpl.encode("gbk", "replace"), media_type="application/octet-stream",
                    headers={"Content-Disposition":
                             "attachment; filename=NasGame-setup.bat"})


# ---------------------------------------------------------------- 旧数据迁移
def _migrate_registry():
    """旧自动发现注册表 → 库内条目: 补 save_tpl(空=待配置), 去掉自动状态/快照字段"""
    reg = _registry()
    changed = False
    for gid, g in list(reg.get("games", {}).items()):
        if not isinstance(g, dict) or not g.get("folder"):
            reg["games"].pop(gid, None)              # 无目录信息的旧空壳
            changed = True
            continue
        if "save_tpl" not in g:
            g["save_tpl"] = ""
            g.setdefault("added_ts", time.time())
            g.pop("status", None)
            g.pop("snapshots", None)
            changed = True
    if changed:
        _save_registry(reg)


_migrate_registry()
