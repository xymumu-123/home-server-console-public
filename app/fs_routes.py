# -*- coding: utf-8 -*-
"""文件管理 API: 全盘浏览/下载/视频流/上传/管理(仅限本地固定磁盘)"""
import ctypes
import hashlib
import json
import mimetypes
import os
import re
import shutil
import string
import subprocess
import threading

import psutil
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from auth import need_perm, CFG

router = APIRouter(prefix="/api/fs")

CHUNK = 512 * 1024
_NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

BASE = os.path.dirname(os.path.abspath(__file__))
THUMB_DIR = os.path.join(BASE, "thumbs")
FFMPEG_BIN = CFG.get("ffmpeg_bin", "")
FFPROBE_BIN = CFG.get("ffprobe_bin", "")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 浏览器 <video> 能直接解码的组合, 其余走 ffmpeg 实时转码
DIRECT_AUDIO = {"aac", "mp3", "opus", "flac", "vorbis"}
DIRECT_VIDEO = {"h264", "hevc", "hvc1", "vp8", "vp9", "av1"}
DIRECT_FORMAT = {"mp4", "mov", "m4v", "webm", "matroska", "mkv"}

_k32 = ctypes.windll.kernel32
DRIVE_FIXED = 3
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def fixed_drives():
    """本机固定磁盘: 用 Win32 API 枚举盘符 (SYSTEM 会话下 psutil.disk_partitions 为空)"""
    fstypes = {}
    try:
        for p in psutil.disk_partitions(all=False):
            fstypes[p.mountpoint] = p.fstype
    except Exception:
        pass
    out = []
    for ch in string.ascii_uppercase:
        drive = ch + ":\\"
        if _k32.GetDriveTypeW(ctypes.c_wchar_p(drive)) != DRIVE_FIXED:
            continue
        if not os.path.exists(drive):
            continue
        rp = os.path.realpath(drive)
        out.append({"path": rp, "fstype": fstypes.get(drive) or fstypes.get(rp) or ""})
    return out


def _require(request: Request):
    """文件接口统一入口: 需要登录 + 「全盘资源」权限 (admin 恒通过)"""
    need_perm(request, "files")


def get_roots():
    return [d["path"] for d in fixed_drives()]


# ---------------------------------------------------------------- 媒体探测/转码
_probe_cache = {}


def probe_media(rp: str):
    """ffprobe 探测音视频编码/时长, 结果按文件版本缓存; 无 ffprobe 返回 None"""
    if not os.path.exists(FFPROBE_BIN):
        return None
    try:
        st = os.stat(rp)
    except OSError:
        return None
    key = f"{rp}|{st.st_mtime_ns}|{st.st_size}"
    if key in _probe_cache:
        return _probe_cache[key]
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", rp],
            capture_output=True, timeout=15, check=True, creationflags=_NO_WINDOW,
        ).stdout
        data = json.loads(out.decode("utf-8", "ignore"))
    except Exception:
        return None
    acodec = vcodec = None
    for s in data.get("streams") or []:
        ct = s.get("codec_type")
        if ct == "audio" and acodec is None:
            acodec = s.get("codec_name")
        elif ct == "video" and vcodec is None:
            vcodec = s.get("codec_name")
    fmt = data.get("format") or {}
    try:
        dur = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    info = {"acodec": acodec, "vcodec": vcodec, "duration": dur,
            "format": fmt.get("format_name", "")}
    if len(_probe_cache) > 300:
        _probe_cache.clear()
    _probe_cache[key] = info
    return info


def play_mode(media) -> str:
    if not media:
        return "direct"
    fmt = set((media.get("format") or "").split(","))
    if (media.get("acodec") in DIRECT_AUDIO
            and (media.get("vcodec") is None or media["vcodec"] in DIRECT_VIDEO)
            and fmt & DIRECT_FORMAT):
        return "direct"
    return "transcode"


def _transcode_stream(rp: str, media: dict, ss: float):
    """实时转码: 音频转 AAC; 视频/容器浏览器不支持时转 H.264, 否则直接拷贝"""
    if not os.path.exists(FFMPEG_BIN):
        raise HTTPException(503, "ffmpeg 未安装")
    args = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, ss):.2f}", "-i", rp]
    vcodec = media.get("vcodec")
    fmt = set((media.get("format") or "").split(","))
    if vcodec:
        args += ["-map", "0:v:0"]
        if vcodec in DIRECT_VIDEO and fmt & DIRECT_FORMAT:
            args += ["-c:v", "copy"]
        else:
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    else:
        args += ["-vn"]
    args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k",
             "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
             "pipe:1"]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except OSError:
        raise HTTPException(503, "ffmpeg 启动失败")

    def gen():
        # 看门狗: 客户端断开后线程池里的 read() 会永久阻塞, 靠定时器杀 ffmpeg 解锁
        try:
            while True:
                watchdog = threading.Timer(30, proc.kill)
                watchdog.daemon = True
                watchdog.start()
                try:
                    chunk = proc.stdout.read(CHUNK)
                finally:
                    watchdog.cancel()
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="video/mp4",
                             headers={"Cache-Control": "no-store"})


def safe_path(path: str) -> str:
    """解析真实路径并确保在本地磁盘范围内, 防路径穿越; 任何解析异常一律拒绝"""
    if not path or len(path) < 2:
        raise HTTPException(400, "invalid path")
    try:
        rp = os.path.realpath(path)
        ok = any(rp.lower().startswith(r.lower().rstrip("\\") + "\\")
                 or rp.lower() == r.lower().rstrip("\\")
                 for r in get_roots())
    except Exception:
        raise HTTPException(403, "path not allowed")
    if not ok:
        raise HTTPException(403, "path not allowed")
    return rp


def valid_name(name: str) -> str:
    if not name or _NAME_BAD.search(name) or name.strip() != name:
        raise HTTPException(400, "invalid name")
    return name


@router.get("/roots")
def roots(request: Request):
    _require(request)
    out = []
    for d in fixed_drives():
        try:
            u = psutil.disk_usage(d["path"])
            out.append({
                "path": d["path"],
                "total": u.total, "used": u.used, "percent": u.percent,
                "fstype": d["fstype"],
            })
        except OSError:
            pass
    return out


@router.get("/list")
def list_dir(request: Request, path: str, hidden: bool = False):
    _require(request)
    rp = safe_path(path)
    if not os.path.isdir(rp):
        raise HTTPException(404, "not a directory")
    dirs, files = [], []
    try:
        with os.scandir(rp) as it:
            for e in it:
                try:
                    # st_file_attributes 来自目录枚举缓存, 对被锁定的系统文件
                    # (pagefile.sys 等)也能拿到, 而 GetFileAttributesW 会被拒绝
                    st = e.stat()
                    if not hidden and getattr(st, "st_file_attributes", 0) & (
                        FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
                    ):
                        continue
                    item = {"name": e.name, "mtime": int(st.st_mtime)}
                    if e.is_dir():
                        dirs.append(item)
                    else:
                        item["size"] = st.st_size
                        files.append(item)
                except OSError:
                    continue
    except PermissionError:
        raise HTTPException(403, "permission denied")
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {"path": rp.replace("\\\\", "\\"), "dirs": dirs, "files": files}


@router.get("/download")
def download(request: Request, path: str):
    _require(request)
    rp = safe_path(path)
    if not os.path.isfile(rp):
        raise HTTPException(404, "not a file")
    return FileResponse(rp, filename=os.path.basename(rp))


@router.get("/raw")
def raw(request: Request, path: str):
    """内联预览 (图片/文本/PDF 等); 可执行内容的类型一律改附件下载防同源 XSS"""
    _require(request)
    rp = safe_path(path)
    if not os.path.isfile(rp):
        raise HTTPException(404, "not a file")
    ctype = mimetypes.guess_type(rp)[0] or "application/octet-stream"
    ext = os.path.splitext(rp)[1].lower()
    headers = None
    if ext in (".html", ".htm", ".svg", ".xml", ".xhtml", ".swf"):
        headers = {"Content-Disposition":
                   f'attachment; filename="{os.path.basename(rp)}"'}
    return FileResponse(rp, media_type=ctype, headers=headers)


def stream_core(request: Request, path: str, ss: float = 0.0, want_info: bool = False):
    """流式播放核心 (无鉴权, 供文件路由与分享路由复用)"""
    rp = safe_path(path)
    if not os.path.isfile(rp):
        raise HTTPException(404, "not a file")
    media = probe_media(rp)
    if want_info:
        # Video.js 需要明确的 MIME 类型; 浏览器不直接支持的容器统一报 video/mp4
        # (Chromium 会按真实内容嗅探播放)
        ctype = mimetypes.guess_type(rp)[0] or ""
        if ctype not in ("video/mp4", "video/webm", "video/ogg"):
            ctype = "video/mp4"
        if media:
            return {"mode": play_mode(media), "acodec": media["acodec"],
                    "vcodec": media["vcodec"], "duration": media["duration"],
                    "ctype": ctype}
        return {"mode": "direct", "acodec": None, "vcodec": None,
                "duration": 0, "ctype": ctype}
    if media and play_mode(media) == "transcode":
        return _transcode_stream(rp, media, ss)

    size = os.path.getsize(rp)
    ctype = mimetypes.guess_type(rp)[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if not m:
            raise HTTPException(416, "bad range")
        start = int(m.group(1)) if m.group(1) else 0
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            raise HTTPException(416, "range error")
    else:
        start, end = 0, size - 1

    length = end - start + 1

    def gen():
        with open(rp, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(gen(), status_code=206, media_type=ctype,
                             headers=headers)


@router.get("/stream")
def stream(request: Request, path: str, info: int = 0, ss: float = 0.0):
    """视频/音频播放: 直接流或 ffmpeg 实时转码; ss=转码起始秒; info=1 返回媒体信息"""
    _require(request)
    return stream_core(request, path, ss, bool(info))


@router.get("/thumb")
def thumb(request: Request, path: str, video: int = 0):
    """缩略图: 图片用 Pillow, 视频用 ffmpeg 抽帧; 缓存到 thumbs/ 目录"""
    _require(request)
    rp = safe_path(path)
    if not os.path.isfile(rp):
        raise HTTPException(404, "not a file")
    try:
        st = os.stat(rp)
        if not video and st.st_size > 200 * 1024 * 1024:
            raise HTTPException(404, "too large")
    except OSError:
        raise HTTPException(404, "not a file")
    key = hashlib.md5(f"{rp}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    cache = os.path.join(THUMB_DIR, key + ".jpg")
    if not os.path.exists(cache):
        os.makedirs(THUMB_DIR, exist_ok=True)
        ok = _video_thumb(rp, cache) if video else _image_thumb(rp, cache)
        if not ok:
            raise HTTPException(404, "no thumb")
    return FileResponse(cache, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=604800"})


def _image_thumb(src: str, dst: str) -> bool:
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((360, 360))
            im.convert("RGB").save(dst, "JPEG", quality=80)
        return True
    except Exception:
        return False


def _video_thumb(src: str, dst: str) -> bool:
    if not os.path.exists(FFMPEG_BIN):
        return False
    try:
        subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-ss", "3",
             "-i", src, "-frames:v", "1", "-vf", "scale=360:-2", "-y", dst],
            timeout=30, capture_output=True, check=True, creationflags=_NO_WINDOW,
        )
        return os.path.exists(dst)
    except Exception:
        return False


UPLOAD_MAX = int(CFG.get("upload_max_gb", 10)) * 1024 * 1024 * 1024   # 单文件上传上限 (GB)


@router.put("/upload")
async def upload(request: Request, path: str, name: str, overwrite: int = 0):
    """浏览器直接 PUT 文件流, 不经 multipart; overwrite=1 时覆盖同名文件 (在线编辑保存用)"""
    _require(request)
    rp = safe_path(path)
    valid_name(name)
    if not os.path.isdir(rp):
        raise HTTPException(404, "target dir missing")
    cl = request.headers.get("content-length")
    # IPv4 备用线路 (frp 有限速): 单文件上限降到 100MB; 客户端自报线路头
    upload_max = 100 * 1024 * 1024 if (request.headers.get("x-client-net") or "").lower() == "v4" \
        else UPLOAD_MAX
    if cl and int(cl) > upload_max:
        raise HTTPException(413, "文件超过大小上限 (IPv4 线路 100MB / IPv6 10GB)")
    dest = os.path.join(rp, name)
    if not overwrite:
        # 同名不覆盖: 自动加序号 (在线编辑保存走 overwrite=1 分支)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(os.path.join(rp, f"{stem} ({i}){ext}")):
                i += 1
            dest = os.path.join(rp, f"{stem} ({i}){ext}")
    tmp = dest + ".uploading"
    total = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > upload_max:
                    raise HTTPException(413, "文件超过大小上限 (IPv4 线路 100MB / IPv6 10GB)")
                f.write(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"ok": True, "saved": os.path.basename(dest)}


@router.post("/mkdir")
async def mkdir(request: Request):
    _require(request)
    body = await request.json()
    rp = safe_path(body.get("path", ""))
    name = valid_name((body.get("name") or "").strip())
    target = os.path.join(rp, name)
    if os.path.exists(target):
        raise HTTPException(409, "already exists")
    os.makedirs(target)
    return {"ok": True}


@router.post("/rename")
async def rename(request: Request):
    _require(request)
    body = await request.json()
    rp = safe_path(body.get("path", ""))
    name = valid_name((body.get("new_name") or "").strip())
    if not os.path.exists(rp):
        raise HTTPException(404, "not found")
    target = os.path.join(os.path.dirname(rp), name)
    if os.path.exists(target):
        raise HTTPException(409, "target name exists")
    os.rename(rp, target)
    return {"ok": True}


def _unique_dest(dst_dir: str, name: str) -> str:
    """目标重名时自动加 (1)/(2) 序号 (与上传行为一致)"""
    dest = os.path.join(dst_dir, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(dst_dir, f"{stem} ({i}){ext}")):
        i += 1
    return os.path.join(dst_dir, f"{stem} ({i}){ext}")


def _move_copy_checks(src: str, dst_dir: str):
    """移动/复制的公共守卫: 自嵌套与整盘"""
    sn = os.path.normpath(src).lower().rstrip("\\")
    dn = os.path.normpath(dst_dir).lower().rstrip("\\")
    if sn == dn or dn.startswith(sn + "\\"):
        raise HTTPException(400, "目标目录位于源目录内部, 不允许该操作")
    if len(sn) <= 3:
        raise HTTPException(400, "不能移动或复制整个盘")


@router.post("/move")
async def fs_move(request: Request):
    """移动文件/文件夹到目标目录 (同盘即时; 跨盘自动复制后删除源)"""
    _require(request)
    body = await request.json()
    src = safe_path(body.get("path", ""))
    dst_dir = safe_path(body.get("target", ""))
    if not os.path.exists(src):
        raise HTTPException(404, "源不存在")
    if not os.path.isdir(dst_dir):
        raise HTTPException(400, "目标目录不存在")
    _move_copy_checks(src, dst_dir)
    dest = _unique_dest(dst_dir, os.path.basename(src.rstrip("\\/")))
    shutil.move(src, dest)
    return {"ok": True, "dest": os.path.basename(dest)}


@router.post("/copy")
async def fs_copy(request: Request):
    """复制文件/文件夹到目标目录 (重名自动加序号)"""
    _require(request)
    body = await request.json()
    src = safe_path(body.get("path", ""))
    dst_dir = safe_path(body.get("target", ""))
    if not os.path.exists(src):
        raise HTTPException(404, "源不存在")
    if not os.path.isdir(dst_dir):
        raise HTTPException(400, "目标目录不存在")
    _move_copy_checks(src, dst_dir)
    dest = _unique_dest(dst_dir, os.path.basename(src.rstrip("\\/")))
    if os.path.isdir(src):
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    return {"ok": True, "dest": os.path.basename(dest)}


@router.post("/delete")
async def delete(request: Request):
    _require(request)
    body = await request.json()
    rp = safe_path(body.get("path", ""))
    if rp in [r.rstrip("\\") for r in get_roots()]:
        raise HTTPException(403, "cannot delete a drive root")
    if os.path.isdir(rp):
        # 系统目录黑名单: rmtree 不可逆, 误删系统目录代价过大
        protected = ("c:\\windows", "c:\\program files", "c:\\program files (x86)",
                     "c:\\programdata", "c:\\users")
        if rp.lower().startswith(protected):
            raise HTTPException(403, "系统目录禁止删除")
        shutil.rmtree(rp)
    elif os.path.isfile(rp):
        os.remove(rp)
    else:
        raise HTTPException(404, "not found")
    return {"ok": True}
