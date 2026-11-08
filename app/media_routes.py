# -*- coding: utf-8 -*-
"""影视库: 扫描/合集分组/列表/播放进度

分组规则 (纯规则零依赖, 只做逻辑分组, 绝不移动磁盘文件):
- media_dirs 一级子文件夹内 ≥2 个视频 → 剧集合集 (文件夹名=剧名, Season/第x季 子文件夹向内折叠)
- 文件夹内仅 1 个视频 → 电影
- 根目录散落视频: 文件名能解析出集数且剧名相同的 ≥2 个 → 归并为剧集
  (剧名 = 文件名去掉集数部分的前缀, 如 "鱿鱼游戏第二季01" → 剧名"鱿鱼游戏" 季2 集1)
- 集数解析优先级: SxxExx > NxM > 第x集/话 > 第x季+集号 > 结尾分隔符+NN (天然排除 19xx/20xx 年份)
- 文件夹内解析不出集数的零散文件 → pending 待整理 (前端引导到文件页整理, 不自动改名/移动)
"""
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request

from auth import need_perm, CFG
from db import get_conn

router = APIRouter()

MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".ts", ".m2ts"}

_media_cache = {"ts": 0.0, "items": []}

# ---------------------------------------------------------------- 集数解析
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_RE_SE = re.compile(r"[Ss](?:eason)?[\s._-]*(\d{1,2})[\s._-]*[Ee][Pp]?[\s._-]*(\d{1,3})")  # S01E02 / Season2 E03
_RE_X = re.compile(r"(?<!\d)(\d{1,2})[xX](\d{1,3})(?!\d)")                        # 1x02
_RE_CN_EP = re.compile(r"第\s*(\d{1,4})\s*[集话話]")                               # 第03集 / 第3话
_RE_CN_SE_EP = re.compile(r"第([一二三四五六七八九十\d]{1,3})季[\s._-]*0*(\d{1,4})")  # 第二季01
_RE_SE_WORD = re.compile(r"第\s*([一二三四五六七八九十\d]{1,3})\s*[季部]|[Ss](\d{1,2})(?!\d)")
_RE_TAIL_EP = re.compile(r"[\s._-]0*(\d{1,3})\s*$")                               # 结尾 -01 / 01
_RE_SEASON_DIR = re.compile(r"^(?:[Ss](?:eason)?[\s._-]*)?(\d{1,2})$|^第\s*(\d{1,2})\s*[季部]$")


def _season_from_stem(stem):
    m = _RE_SE_WORD.search(stem)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    return _CN_NUM.get(raw) or (int(raw) if raw and raw.isdigit() else None)


def _season_hint(subdirs):
    """文件名无季号时, 回看 Season 2 / S02 / 第二季 样式的子文件夹名 (内层优先)"""
    for d in reversed(subdirs):
        m = _RE_SEASON_DIR.match(d.strip())
        if m:
            return int(m.group(1) or m.group(2))
    return None


def _parse_video(stem, subdirs):
    """从文件名解析 (季, 集, 剧名); 解析不出返回 None。剧名 = 匹配段之前的前缀"""
    m = _RE_SE.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2)), stem[:m.start()].strip(" .-_")
    m = _RE_X.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2)), stem[:m.start()].strip(" .-_")
    m = _RE_CN_EP.search(stem)
    if m:
        return (_season_hint(subdirs) or _season_from_stem(stem) or 1,
                int(m.group(1)), stem[:m.start()].strip(" .-_"))
    m = _RE_CN_SE_EP.search(stem)
    if m:
        s_raw = m.group(1)
        season = _CN_NUM.get(s_raw) or (int(s_raw) if s_raw.isdigit() else 1)
        return season, int(m.group(2)), stem[:m.start()].strip(" .-_")
    m = _RE_TAIL_EP.search(stem)
    if m:
        return (_season_hint(subdirs) or _season_from_stem(stem) or 1,
                int(m.group(1)), stem[:m.start()].strip(" .-_"))
    return None


# ---------------------------------------------------------------- 扫描
def media_scan():
    if time.time() - _media_cache["ts"] < 30:
        return _media_cache["items"]
    out, seen = [], set()
    for root_dir in CFG.get("media_dirs", []):
        if not os.path.isdir(root_dir):
            continue
        for cur, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in ("$RECYCLE.BIN", "System Volume Information")]
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in MEDIA_EXTS or fn.endswith(".uploading"):
                    continue
                full = os.path.join(cur, fn)
                if full.lower() in seen:   # 多个扫描目录有嵌套时防重复
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                seen.add(full.lower())
                out.append({"path": full, "name": os.path.splitext(fn)[0],
                            "size": st.st_size, "mtime": int(st.st_mtime)})
    _media_cache.update({"ts": time.time(), "items": out})
    return out


def _watched(p):
    return bool(p and p["duration"] and p["position"] >= p["duration"] * 0.95)


def _make_collection(key, name, eps, prog):
    """把同一部剧的集数列表包装成合集 (挂进度/算已看/选续播集)"""
    for e in eps:
        e["label"] = "S%02dE%02d" % (e["season"], e["episode"])
        e["progress"] = prog.get(e["path"])
    watched = sum(1 for e in eps if _watched(e["progress"]))
    watching = [e for e in eps if e["progress"] and e["progress"]["position"] > 60
                and e["progress"]["duration"]
                and e["progress"]["position"] < e["progress"]["duration"] * 0.95]
    if watching:
        c = max(watching, key=lambda e: e["progress"]["updated_at"])
    else:
        c = next((e for e in eps if not e["progress"]), None)   # 没在看 → 下一集未看过的
    cont = None
    if c:
        cont = {"path": c["path"], "label": c["label"], "season": c["season"],
                "position": c["progress"]["position"] if c["progress"] else 0,
                "duration": c["progress"]["duration"] if c["progress"] else 0}
    return {"key": key, "name": name, "episodes": eps,
            "seasons": sorted({e["season"] for e in eps}),
            "total": len(eps), "watched": watched, "cont": cont}


@router.get("/api/media/list")
def media_list(request: Request):
    need_perm(request)
    items = media_scan()
    roots = [os.path.normpath(d) for d in CFG.get("media_dirs", [])]

    # 1) 按一级子文件夹归堆; 根目录散文件单独一组
    folders, loose = {}, []
    for it in items:
        full, low = it["path"], it["path"].lower()
        col = None
        for r in roots:
            rl = r.lower() + os.sep
            if low.startswith(rl):
                rest = full[len(r) + 1:]
                if os.sep in rest:                     # 在子目录里 → 归属一级文件夹
                    col = os.path.join(r, rest.split(os.sep)[0])
                break                                   # 根目录散文件: col 保持 None
        if col:
            folders.setdefault(col, []).append(it)
        else:
            loose.append(it)

    conn = get_conn()
    rows = conn.execute(
        "SELECT path, position, duration, updated_at FROM watch_progress").fetchall()
    conn.close()
    prog = {r[0]: {"position": r[1], "duration": r[2], "updated_at": r[3]} for r in rows}

    collections, movies, pending = [], [], []

    # 2) 文件夹组: ≥2 视频 → 合集; 1 视频 → 电影; 集数全解析不出 → 整夹待整理
    for col, vids in sorted(folders.items()):
        if len(vids) == 1:
            movies.append(vids[0])
            continue
        dirname = os.path.basename(col)
        eps, extra = [], []
        for v in vids:
            rel = os.path.relpath(v["path"], col)
            subdirs = rel.split(os.sep)[:-1]
            stem = os.path.splitext(os.path.basename(v["path"]))[0]
            parsed = _parse_video(stem, subdirs)
            if parsed:
                season, ep, _prefix = parsed
                eps.append(dict(v, season=season, episode=ep))
            else:
                extra.append(v)
        if not eps:
            for v in extra:
                pending.append(dict(v, folder=col, reason="无法识别集数"))
            continue
        eps.sort(key=lambda x: (x["season"], x["episode"], x["name"]))
        for v in extra:
            pending.append(dict(v, folder=col, reason="未能识别集数, 未计入剧集"))
        collections.append(_make_collection(col, dirname, eps, prog))

    # 3) 散文件: 能解析集数的按剧名归并 (同名 ≥2 才成合集, 单个退回电影)
    by_name = {}
    for it in loose:
        stem = os.path.splitext(os.path.basename(it["path"]))[0]
        parsed = _parse_video(stem, [])
        if parsed and parsed[2]:
            season, ep, name = parsed
            by_name.setdefault(name, []).append(dict(it, season=season, episode=ep))
        else:
            movies.append(it)
    for name, eps in by_name.items():
        if len(eps) < 2:
            movies.append(eps[0])
            continue
        eps.sort(key=lambda x: (x["season"], x["episode"], x["name"]))
        collections.append(_make_collection("loose:" + name, name, eps, prog))

    # 4) 进度挂接 + 稳定排序
    movies = [dict(m, progress=prog.get(m["path"])) for m in movies]
    movies.sort(key=lambda x: x["name"].lower())
    collections.sort(key=lambda x: x["name"].lower())
    pending = [dict(p, progress=prog.get(p["path"])) for p in pending]
    pending.sort(key=lambda x: (x["folder"], x["name"]))
    return {"collections": collections, "movies": movies, "pending": pending}


@router.post("/api/media/progress")
async def media_progress(request: Request):
    need_perm(request)
    body = await request.json()
    path = body.get("path") or ""
    try:
        position = float(body.get("position") or 0)
        duration = float(body.get("duration") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "bad numbers")
    if not path or position < 0 or duration < 0:
        raise HTTPException(400, "bad request")
    rp = os.path.realpath(path)
    if not any(rp.lower().startswith(d.lower().rstrip("\\") + "\\") for d in CFG.get("media_dirs", [])):
        raise HTTPException(403, "not a media dir")
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO watch_progress VALUES (?,?,?,?)",
                 (rp, position, duration, time.time()))
    conn.commit()
    conn.close()
    return {"ok": True}
