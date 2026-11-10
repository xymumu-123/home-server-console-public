# -*- coding: utf-8 -*-
"""统一配置模块: 全部配置来自项目根的 config.ini (INI, 带中文注释)。

- SCHEMA 是唯一事实来源: 每个键声明 (类型, 默认值, 是否必需, 中文说明),
  config.ini / config.example.ini 均由本模块的 generate_ini() 生成, 保证文档一致
- load(): 解析并校验, 返回与旧 config.json 同构的 CFG 字典 (各模块无需改动取值方式)
  * 缺少必需键 / 类型不合法 → RuntimeError, 报错信息指明 [节] 键与原因
  * 缺少可选键 → 自动填入默认值
- save_updates({(节, 键): 文本}): 定向替换 INI 中对应行的值, 保留全部注释 (原子写)
- 热更新: 需要每次重读的模块 (ddns) 可重复调用 load(), 成本可忽略
"""
import configparser
import json
import os
import secrets
import sys
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(os.path.dirname(BASE), "config.ini")     # 项目根

T_STR, T_INT, T_FLOAT, T_BOOL, T_JSON, T_LIST = (
    "str", "int", "float", "bool", "json", "list")

# ---------------------------------------------------------------- 配置模式
# {节: {键: (类型, 默认值, 是否必需, 中文说明)}}
SCHEMA = {
    "server": {
        "listen_host":   (T_STR,  "0.0.0.0", False, "监听地址: 0.0.0.0=所有网卡, 127.0.0.1=仅本机"),
        "listen_port":   (T_INT,  8000,      False, "监听端口 (局域网直接访问; 外网经端口映射/FRP 转发)"),
        "ssl_cert":      (T_STR,  "",        False, "TLS 证书 fullchain PEM 路径 (与 ssl_key 同时配置时, 127.0.0.1:8443 启用 HTTPS; 留空=纯 HTTP)"),
        "ssl_key":       (T_STR,  "",        False, "TLS 证书私钥 PEM 路径 (配 ssl_cert 使用; 由 acme/acme_issue.py 签发续期)"),
        "secret_key":    (T_STR,  "",        False, "会话签名密钥 (64位十六进制)。留空则首次启动自动生成; 更换后所有登录会话失效"),
    },
    "auth": {
        "token_days":           (T_INT,  30,     False, "登录会话有效期 (天)"),
        "pbkdf2_iters":         (T_INT,  120000, False, "密码哈希迭代次数 (改小会降低安全性, 仅旧机器性能不足时调整)"),
        "admin_name":           (T_STR,  "admin", False, "管理员账号名 (不可删除, 权限固定全部)"),
        "initial_admin_password": (T_STR, "",    False, "首次部署时 admin 的初始密码 (账号已存在时本项无效; 留空则自动生成随机密码并打印在启动日志)"),
    },
    "monitor": {
        "sample_interval":     (T_INT,   2,    False, "监控采样间隔 (秒)"),
        "baseline_power_w":    (T_FLOAT, 15.0, False, "整机功率估算的基础功耗 (瓦), 实际功率 = CPU 功耗 + 本值"),
        "long_retention_days": (T_INT,   365,  False, "监控历史最长保留天数 (自动分级聚合)"),
    },
    "paths": {
        "media_dirs":   (T_LIST, "D:\\NAS\\Videos", False, "影视库扫描目录, 逗号分隔多个"),
        "game_dirs":    (T_LIST, "D:\\Games, D:\\Games", False, "游戏库扫描根目录, 逗号分隔多个"),
        "download_dir": (T_STR,  "D:\\NAS\\Downloads", False, "下载机默认下载目录"),
    },
    "games": {
        "games_token":      (T_STR, "", False, "游戏客户端令牌 (setup.bat 与启动器内嵌, 留空则首次启动自动生成; 更换后客户端需重新接入)"),
        "games_server_url": (T_STR, "", False, "客户端可访问的服务器地址 (内外网均可达, 如 http://pc2.example.com:18002), 留空则用请求地址"),
        "games_unc_base":   (T_STR, "", False, "旧版单目录 UNC 映射 (兼容保留, 推荐用 games_unc_map)"),
        "games_unc_map":    (T_JSON, {}, False, "本地路径→UNC 前缀映射 (JSON 对象), 客户端启动游戏时把服务器路径替换为可访问的共享路径"),
        "smb_user":         (T_STR, "", False, "SMB 专用账户名 (留空则首次启动自动创建为 smbgame; 客户端凭它只读访问游戏共享, 避免存管理员密码)"),
        "smb_pass":         (T_STR, "", False, "SMB 专用账户密码 (留空则首次启动自动生成随机密码; 改动后需以管理员运行 D:\\Monitor\\_smb_account.ps1 同步 Windows 账户)"),
        "frp_url":          (T_STR, "", False, "IPv4 备用入口 (SakuraFRP 隧道地址), 供前端提示与切换"),
        "lease_hours":      (T_INT, 6,  False, "游戏租约时长 (小时), 防止两台电脑同时玩同一游戏覆盖存档"),
        "cover_max_mb":     (T_INT, 8,  False, "游戏封面上限 (MB)"),
        "games_root":       (T_STR, "", False, "旧版单扫描根 (兼容保留, 推荐用 game_dirs)"),
        "saves_root":       (T_STR, "", False, "游戏存档快照根目录 (如 D:\\saves), 留空则游戏库存档功能不可用"),
    },
    "ai": {
        "ai_api_key":  (T_STR, "", False, "DeepSeek API 密钥 (platform.deepseek.com 申请; AI 助手功能的模型后端)"),
        "ai_model":    (T_STR, "deepseek-chat", False, "AI 助手使用的模型 (deepseek-chat=通用快速, deepseek-reasoner=深度推理)"),
        "ai_dsh_bin":  (T_STR, "", False, "dsh 智能体入口 bin.js 完整路径 (留空则自动探测; 留空找不到时需手动填写)"),
    },
    "media": {
        "ffmpeg_bin":  (T_STR, "", False, "ffmpeg.exe 完整路径 (缩略图/转码, 留空则相关功能不可用)"),
        "ffprobe_bin": (T_STR, "", False, "ffprobe.exe 完整路径 (媒体信息探测, 留空则按直连播放)"),
    },
    "aria2": {
        "aria2_bin":    (T_STR, "", False, "aria2c.exe 完整路径 (留空=不启用下载机)"),
        "aria2_port":   (T_INT, 6800, False, "aria2 RPC 端口 (仅本机访问)"),
        "aria2_secret": (T_STR, "", False, "aria2 RPC 密钥"),
    },
    "ddns": {
        "enabled":      (T_BOOL, True,  False, "是否启用 DDNS 自动更新"),
        "domain":       (T_STR,  "",    False, "DDNS 域名 (如 pc2.example.com)"),
        "interval_min": (T_INT,  1,     False, "检查间隔 (分钟)"),
        "api_url":      (T_STR,  "",    False, "DNS 服务商回调接口地址"),
        "api_key":      (T_STR,  "",    False, "回调参数模板, #{domain} 与 #{ip} 会被替换 (本项值内允许出现特殊字符)"),
        "ip_urls":      (T_LIST, "https://v6.ident.me, https://6.ipw.cn, https://api6.ipify.org", False, "公网 IP 检测源, 逗号分隔多个"),
    },
    "probe": {
        "v6_port":        (T_INT, 18002, False, "用户面 IPv6 端口 (前端线路探测/切换跳转用)"),
        "down_max_mb":    (T_INT, 25,    False, "测速单次下载块上限 (MB)"),
        "up_max_mb":      (T_INT, 25,    False, "测速单次上传块上限 (MB)"),
        "volume_limit_mb": (T_INT, 3072, False, "测速每 IP 每分钟流量上限 (MB)"),
        "rate_per_min":   (T_INT, 120,   False, "探针每 IP 每分钟请求次数上限"),
    },
    "fs": {
        "upload_max_gb": (T_INT, 10, False, "文件上传单文件上限 (GB); IPv4 备用线路自动降为 100MB"),
    },
    "interface": {
        "wol_devices":  (T_JSON, [], False, "局域网唤醒设备列表 (JSON 数组, 由网页界面管理, 一般无需手改)"),
        "quick_links":  (T_JSON, [], False, "中控页外部链接卡片 (JSON 数组: [{name,url,icon}])"),
    },
}

# 键名 → 节 (构建扁平 CFG 与定向回写时使用)
_KEY2SEC = {k: sec for sec, keys in SCHEMA.items() for k in keys}

# INI 读写锁: 防止设置保存 / WOL 保存 / 密钥自动生成 并发改坏文件
_io_lock = threading.RLock()


class ConfigError(RuntimeError):
    """配置错误 (缺失/类型不合法), 报错信息面向使用者"""


# ---------------------------------------------------------------- 类型转换
def _conv(raw, typ, sec, key):
    """把 INI 里的原始字符串转换为配置值; 出错抛出带定位的 ConfigError"""
    where = f"[{sec}] {key}"
    try:
        if typ == T_STR:
            return raw.strip()          # 路径/URL/令牌不应含首尾空白 (手改 INI 防呆)
        if typ == T_INT:
            return int(raw)
        if typ == T_FLOAT:
            return float(raw)
        if typ == T_BOOL:
            low = raw.strip().lower()
            if low in ("1", "true", "on", "yes", "是", "开"):
                return True
            if low in ("0", "false", "off", "no", "否", "关"):
                return False
            raise ValueError(f"无法识别的布尔值: {raw!r} (可用: true/false/1/0/是/否)")
        if typ == T_LIST:
            return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
        if typ == T_JSON:
            v = json.loads(raw)
            if not isinstance(v, (list, dict)):
                raise ValueError("必须是 JSON 数组或对象")
            return v
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"配置错误: {where}: {e} (原始值: {raw!r})") from e
    raise ConfigError(f"配置错误: {where}: 未知类型 {typ}")


def _fmt(val, typ):
    """把 Python 值格式化回 INI 文本 (写回用)"""
    if typ in (T_JSON,):
        return json.dumps(val, ensure_ascii=False)
    if typ == T_LIST:
        return ", ".join(val)
    if typ == T_BOOL:
        return "true" if val else "false"
    return str(val)


# ---------------------------------------------------------------- 加载
def load():
    """解析 config.ini → CFG 字典 (与旧 config.json 同构); 带完整校验"""
    if not os.path.exists(INI_PATH):
        raise ConfigError(
            f"配置文件不存在: {INI_PATH}\n"
            f"请复制 config.example.ini 为 config.ini 后按需修改, 或查看 README 的快速开始章节")
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(INI_PATH, encoding="utf-8-sig") as f:
            cp.read_file(f)
    except configparser.Error as e:
        raise ConfigError(f"配置文件格式错误: {INI_PATH}: {e}") from e

    cfg = {}
    missing_required = []
    for sec, keys in SCHEMA.items():
        for key, (typ, default, required, _desc) in keys.items():
            where = f"[{sec}] {key}"
            if cp.has_option(sec, key):
                raw = cp.get(sec, key)
                if typ != T_STR:
                    raw = raw.strip()
                cfg[key] = _conv(raw, typ, sec, key)
            elif required:
                missing_required.append(where)
            else:
                cfg[key] = default
    if missing_required:
        raise ConfigError("配置缺失 (请在 config.ini 中补充):\n  " + "\n  ".join(missing_required))

    # 派生结构: ddns 子字典 (ddns.py 按 CFG['ddns'] 取值)
    cfg["ddns"] = {k: cfg[k] for k in SCHEMA["ddns"]}
    # 自动生成类密钥: 留空则生成并写回 INI (持锁 + 双检, 防并发重复生成)
    with _io_lock:
        _ensure_secrets(cfg)
    return cfg


def _ensure_secrets(cfg):
    """secret_key / games_token / smb 账户为空时生成并写回; 先重读 INI 双检 (另一线程可能已写入)"""
    import string
    gens = {"secret_key": lambda: secrets.token_hex(32),
            "games_token": lambda: secrets.token_hex(16),
            # SMB 专用账户: 用户名固定, 密码纯字母数字 (net user / cmdkey 免转义)
            "smb_user": lambda: "smbgame",
            "smb_pass": lambda: "".join(secrets.choice(string.ascii_letters + string.digits)
                                        for _ in range(20))}
    need = [k for k, gen in gens.items() if not cfg.get(k)]
    if not need:
        return
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(INI_PATH, encoding="utf-8-sig")
    except Exception:
        cp = None
    for key in list(need):
        sec = _KEY2SEC[key]
        if cp is not None and cp.has_option(sec, key) and cp.get(sec, key).strip():
            cfg[key] = cp.get(sec, key)
            need.remove(key)
    for key in need:
        cfg[key] = gens[key]()
        save_updates({key: cfg[key]})
        print(f"[config] {key} 为空, 已自动生成并写入 config.ini", flush=True)


# ---------------------------------------------------------------- 定向回写 (保留注释)
def save_updates(mapping):
    """把 {(节,键): 值} 定向写回 config.ini, 只改对应行的值, 保留全部注释与其他内容"""
    if not mapping:
        return
    # 值按 schema 类型格式化
    fmted = {}
    for k, val in mapping.items():
        sec = _KEY2SEC.get(k)
        if not sec:
            raise ConfigError(f"未知配置键: {k}")
        typ, _def, _req, _desc = SCHEMA[sec][k]
        fmted[(sec, k)] = _fmt(val, typ)

    with _io_lock:
        with open(INI_PATH, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()

        pending = dict(fmted)
        cur_sec = None
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                cur_sec = s[1:-1].strip()
                continue
            if cur_sec and "=" in s and not s.startswith(("#", ";")):
                key_text = s.split("=", 1)[0].strip()
                # 大小写不敏感匹配 (用户手改大小写时仍命中同一行, 不产生重复键)
                # 注意: pending 里的值已是格式化文本, 此处严禁再次 _fmt (会字符拼接/二次编码)
                if (cur_sec, key_text.lower()) in pending:
                    lines[i] = f"{key_text} = {pending.pop((cur_sec, key_text.lower()))}"
        # 未命中的键: 追加到对应节尾 (节不存在则追加节); 值已是格式化文本
        for (sec, key), val in pending.items():
            sec_idx = next((i for i, ln in enumerate(lines)
                            if ln.strip() == f"[{sec}]"), None)
            line = f"{key} = {val}"
            if sec_idx is None:
                lines += ["", f"[{sec}]", line]
            else:
                j = sec_idx + 1
                while j < len(lines) and not lines[j].strip().startswith("["):
                    j += 1
                lines.insert(j, line)

    tmp = INI_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, INI_PATH)


def save_ui_keys(cfg):
    """把界面改动 (扫描目录/WOL 设备) 定向写回 config.ini: 先备份 .bak, 保留注释"""
    import shutil
    if os.path.exists(INI_PATH):
        try:
            shutil.copy2(INI_PATH, INI_PATH + ".bak")
        except OSError:
            pass
    keys = {k: cfg[k] for k in ("media_dirs", "game_dirs", "wol_devices", "quick_links")
            if k in cfg}
    save_updates(keys)


# ---------------------------------------------------------------- 首次生成
def generate_ini(dst, values=None, comments=True):
    """按 SCHEMA 生成 INI 文件; values 提供真实值 (迁移用), 缺省用默认值"""
    values = values or {}
    lines = []
    if comments:
        lines.append("# 家庭服务器中控台配置文件")
        lines.append("# 说明: 每个键的含义见各行注释; 修改后重启服务生效")
        lines.append("# 本文件含密钥等敏感信息, 已加入 .gitignore, 请勿上传")
        lines.append("")
    for sec, keys in SCHEMA.items():
        if comments:
            lines.append(f"[{sec}]")
        for key, (typ, default, required, desc) in keys.items():
            val = values.get(key, default)
            if comments:
                lines.append(f"# {desc}")
            lines.append(f"{key} = {_fmt(val, typ)}")
            if comments:
                lines.append("")
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    try:
        c = load()
        print("config.ini 加载成功, 键数:", len(c))
    except ConfigError as e:
        print("配置错误:", e, file=sys.stderr)
        sys.exit(1)
