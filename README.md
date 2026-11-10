# 家庭服务器中控台

一个运行在 Windows 家庭服务器上的自托管 Web 控制台：监控、影视库、文件管理、下载机、游戏库、DDNS 与多账号权限管理，全部集成在一个 FastAPI 应用里。

> 设计目标：局域网与公网（IPv6 直连 + IPv4 FRP 备用）都能访问；网页之外零客户端依赖；
> 游戏存档跨电脑漫游；一切配置集中在 `config.ini`。

## 功能特性

- **中控首页**：CPU / 内存 / 温度 / 功耗 / 网络实时状态，服务磁贴与外部链接卡片
- **性能监控**：2 秒级采样，24 小时～1 年自动分级聚合的历史曲线（监控数据按保留期自动清理）
- **影视库**：扫描媒体目录生成海报墙、剧集自动归组合集、续播记录、在线播放（弱机自动转码）
- **文件管理**：全盘浏览、上传下载、传输队列、在线编辑（txt/md）与 docx/xlsx/pptx 预览、分享链接（可设提取码与有效期）
- **下载机**：基于 aria2 的 HTTP / FTP / 磁力 / BT 离线下载，任务自动恢复
- **游戏库**：扫描游戏目录 → 人工配置上架（封面/主程序/存档路径模板），一键启动 + 存档跨电脑漫游 + 租约防冲突
- **DDNS**：IPv6 AAAA 记录自动更新（西部数码回调接口），外网随时可达
- **IPv4 备用线路**：SakuraFrp 内网穿透备用入口，进站自动检测线路并提供一键切换
- **网络测速**：仪表盘式 IPv6 带宽测速（下载/上传/时延/抖动）并给出使用建议
- **磁盘健康**：物理盘 SMART 温度/通电时长/健康状态，异常自动在首页标警
- **全站 HTTPS**：Let's Encrypt 证书 DNS-01 自动签发续期，双协议端口并存
- **多账号权限**：5 个权限位（人员/游戏/全盘资源/系统高级/下载管理），账号增删改全在网页完成

## 技术栈

- 后端：Python 3.9+ / FastAPI / uvicorn / SQLite (WAL) / psutil
- 前端：原生 HTML/JS/CSS（无构建步骤），Video.js 播放器
- 传感器：PowerShell + LibreHardwareMonitorLib（温度/功耗，Windows 专用）
- 下载：aria2（RPC 由后端代理，端口不对外暴露）
- 部署：Windows 计划任务（SYSTEM 自启）/ `deploy.bat` 一键脚本

## 目录结构

```
Monitor/
├── app/                      # 应用主体
│   ├── app.py                # FastAPI 装配 + 页面路由 / 监控采样 / 设置 / 人员管理
│   ├── auth.py               # 登录鉴权 + 多账号权限 (pbkdf2, users 表)
│   ├── app_config.py         # 统一配置模块 (config.ini 的加载/校验/回写)
│   ├── db.py                 # SQLite 公共层 (建表/连接/KV)
│   ├── games.py              # 游戏库：发现/门控 API/租约/存档快照/客户端生成
│   ├── fs_routes.py          # 文件管理 API (浏览/上传/下载/流媒体/缩略图)
│   ├── share_routes.py       # 链接分享 (提取码 POST + 签名 Cookie, 公开访问页)
│   ├── dl_routes.py          # 下载机 (aria2 进程管理/RPC 代理/任务 CRUD)
│   ├── power_routes.py       # 电源控制 + 局域网唤醒
│   ├── media_routes.py       # 影视库 (扫描/列表/播放进度)
│   ├── probe_routes.py       # 连通性探针 + 测速 (CORS, 免鉴权)
│   ├── ddns.py               # DDNS 后台线程 (独立模块)
│   ├── sensor_daemon.ps1     # 传感器守护 (PowerShell + LibreHardwareMonitor)
│   ├── sensor_watchdog.ps1   # 传感器看门狗 (挂死自动重启)
│   ├── launcher_template.ps1 # 游戏客户端启动器模板 (自更新)
│   ├── setup_template.bat    # 游戏客户端接入脚本模板
│   ├── config.json           # 旧版配置 (历史参考, 已不读取)
│   ├── data.db               # SQLite (账号/监控历史/观影进度/分享)
│   └── static/               # 全部前端页面与资源
├── acme/                     # Let's Encrypt 证书签发/续期脚本 (DNS-01)
├── certs/                    # TLS 证书与私钥 (自动生成, 勿外传)
├── _smb_account.ps1          # SMB 专用账户配置脚本 (建号/对齐密码/授权)
├── _restart_web.ps1          # 服务重启辅助脚本
├── LibreHardwareMonitor/     # 温度/功耗组件 (可选)
├── aria2/                    # 下载机组件 (可选)
├── ffmpeg/                   # 转码组件 (可选, 供低性能设备播放)
├── config.ini                # 运行配置 (由 config.example.ini 生成, 含密钥勿上传)
├── config.example.ini        # 配置模板
├── requirements.txt          # Python 依赖
## HTTPS (已启用)

- 主线路 `https://pc2.jzlspace.top:18002`（Let's Encrypt 证书, DNS-01 验证）
- 签发/续期: `python acme/acme_issue.py`（计划任务 MonitorAcmeRenew 每天 05:30 自动跑, 剩余 30 天内才真正重签, 续期后自动重启服务）
- 证书文件在 `certs/`（gitignore）, 账户密钥 `acme/account.key`（勿删勿传, 删了要重新注册账户）
- 服务端: `[server] ssl_cert/ssl_key` 两键控制, **留空 = 回滚纯 HTTP**（portproxy 指回 8000）
- 端口分工: 8000 = HTTP 局域网; 127.0.0.1:8443 = HTTPS（portproxy 18002 进入, 不对局域网暴露）
- cookie 未加 Secure: 两协议登录态互通; 退役 HTTP 时再加 Secure/HSTS

├── deploy.bat / deploy.sh    # 一键部署脚本
├── install_admin.bat         # 一次性管理员安装 (计划任务/端口映射/防火墙)
└── 重启监控服务.bat           # 手动重启服务的辅助脚本
```

## 环境要求

- Windows 10/11 或 Windows Server（温度/功耗功能依赖 Windows；Linux 下核心功能可用）
- Python 3.9 及以上
- （可选）LibreHardwareMonitor：温度/功耗/风扇
- （可选）aria2 1.36+：下载机
- （可选）ffmpeg：视频缩略图与弱设备转码
- 局域网路由器支持 IPv6，或已有 SakuraFrp 账号（公网 IPv4 备用入口）

## 快速开始（一键部署）

### Windows

1. 把整个项目目录放到任意位置（如 `D:\Monitor`）
2. 双击运行 `deploy.bat` —— 自动完成：环境检查 → 安装依赖 → 生成 `config.ini` → 启动
3. 按启动日志里的初始密码登录：`http://127.0.0.1:8000/`（账号 `admin`）
4. 按需修改 `config.ini` 后重启 `deploy.bat` 生效

### Linux / Mac

```bash
bash deploy.sh
```

> Linux 下温度/功耗组件（LibreHardwareMonitor）不可用，其余功能完整。

### 开机自启（可选，Windows）

以管理员运行 `install_admin.bat`：注册 SYSTEM 计划任务（MonitorWeb /
MonitorSensors / MonitorSensorWatch）、IPv6 端口映射（18002 → 8000）与防火墙规则，
实现开机免登录自动运行。

## 配置文件详解（config.ini）

> 首次部署由 `config.example.ini` 复制生成；`config.ini` 含密钥，已加入 `.gitignore`。
> 修改后重启服务生效；网页「设置」里修改的扫描目录会自动写回本文件（保留注释）。

### [server] 服务

| 键 | 默认 | 说明 |
|---|---|---|
| `listen_host` | `0.0.0.0` | 监听地址：`0.0.0.0`=所有网卡，`127.0.0.1`=仅本机 |
| `listen_port` | `8000` | 监听端口（局域网直接访问；公网经端口映射/FRP 转发） |
| `secret_key` | 自动生成 | 会话签名密钥（64 位十六进制）；更换后所有登录会话失效 |

### [auth] 账号

| 键 | 默认 | 说明 |
|---|---|---|
| `token_days` | `30` | 登录会话有效期（天） |
| `pbkdf2_iters` | `120000` | 密码哈希迭代次数 |
| `admin_name` | `admin` | 管理员账号名（不可删除） |
| `initial_admin_password` | 空 | 首次部署 admin 的初始密码；留空则随机生成并打印在启动日志 |

### [monitor] 监控采样

| 键 | 默认 | 说明 |
|---|---|---|
| `sample_interval` | `2` | 采样间隔（秒） |
| `baseline_power_w` | `15` | 功率估算基础功耗（瓦）：实际功率 = CPU 功耗 + 本值 |
| `long_retention_days` | `365` | 监控历史最长保留天数（超期自动分级聚合） |

### [paths] 扫描与存储目录

| 键 | 默认 | 说明 |
|---|---|---|
| `media_dirs` | `D:\NAS\Videos` | 影视库扫描目录，逗号分隔多个 |
| `game_dirs` | `G:\MoveNas\game, H:\GameNas` | 游戏库扫描根目录，逗号分隔多个 |
| `download_dir` | `D:\NAS\Downloads` | 下载机默认下载目录 |

### [games] 游戏库

| 键 | 默认 | 说明 |
|---|---|---|
| `games_token` | 自动生成 | 客户端令牌（内嵌于接入脚本与启动器；更换后客户端需重新接入） |
| `games_server_url` | 空 | 客户端可访问的服务器地址（内外网均可达），留空用请求地址 |
| `games_unc_base` | 空 | 旧版单目录 UNC 映射（兼容保留，推荐 `games_unc_map`） |
| `games_unc_map` | `{}` | 本地路径 → UNC 前缀映射（JSON 对象），客户端启动游戏时替换为共享路径 |
| `smb_user` | 自动生成 | SMB 专用只读账户名（客户端凭据自动登记用，避免存管理员密码） |
| `smb_pass` | 自动生成 | SMB 专用账户密码（服务启动时自动创建同名 Windows 账户；变更后以管理员运行 `_smb_account.ps1` 对齐） |
| `frp_url` | 空 | IPv4 备用入口地址（SakuraFrp 隧道），供前端提示与一键切换 |
| `lease_hours` | `6` | 游戏租约时长（小时），防止两台电脑同时玩同一游戏覆盖存档 |
| `cover_max_mb` | `8` | 游戏封面上限（MB） |
| `games_root` | 空 | 旧版单扫描根（兼容保留，推荐 `game_dirs`） |
| `saves_root` | 空 | 游戏存档快照根目录（如 `G:\movenas\saves`） |

### [media] 转码组件

| 键 | 默认 | 说明 |
|---|---|---|
| `ffmpeg_bin` | 空 | ffmpeg.exe 完整路径（缩略图/转码，留空则相关功能不可用） |
| `ffprobe_bin` | 空 | ffprobe.exe 完整路径（媒体信息探测） |

### [aria2] 下载机

| 键 | 默认 | 说明 |
|---|---|---|
| `aria2_bin` | 空 | aria2c.exe 完整路径（留空 = 不启用下载机） |
| `aria2_port` | `6800` | aria2 RPC 端口（仅本机访问，不对外暴露） |
| `aria2_secret` | 空 | aria2 RPC 密钥 |

### [ddns] 动态域名

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用 DDNS 自动更新 |
| `domain` | 空 | DDNS 域名（如 `pc2.example.com`） |
| `interval_min` | `1` | 检查间隔（分钟） |
| `api_url` | 空 | DNS 服务商回调接口地址 |
| `api_key` | 空 | 回调参数模板，`#{domain}` 与 `#{ip}` 会被替换 |
| `ip_urls` | 两个公共源 | 公网 IP 检测源，逗号分隔多个 |

### [probe] 连通性探针与测速

| 键 | 默认 | 说明 |
|---|---|---|
| `v6_port` | `18002` | 用户面 IPv6 端口（前端线路探测/切换跳转用） |
| `down_max_mb` | `25` | 测速单次下载块上限（MB） |
| `up_max_mb` | `25` | 测速单次上传块上限（MB） |
| `volume_limit_mb` | `3072` | 测速每 IP 每分钟流量上限（MB） |
| `rate_per_min` | `120` | 探针每 IP 每分钟请求次数上限 |

### [fs] 文件管理

| 键 | 默认 | 说明 |
|---|---|---|
| `upload_max_gb` | `10` | 上传单文件上限（GB）；IPv4 备用线路自动降为 100MB |

### [interface] 界面数据

| 键 | 默认 | 说明 |
|---|---|---|
| `wol_devices` | `[]` | 局域网唤醒设备（JSON 数组，由网页管理，无需手改） |
| `quick_links` | `[]` | 中控页外部链接卡片（JSON 数组：`[{"name":"...","url":"...","icon":"..."}]`） |

## 游戏库使用流程

1. 把游戏文件夹放入任一 `game_dirs` 扫描目录
2. 游戏库页面「待添加」区会出现新游戏 → 点「添加」
3. 配置显示名、封面、启动主程序与**存档路径模板**（支持 `%USERPROFILE%` 等
   Windows 原生变量，每台电脑自动展开）
4. 保存后即可从游戏库一键启动；存档在启动前拉取、退出后自动回传
5. 每台客户端电脑只需运行一次页面上的 `NasGame-setup.bat` 接入；
   启动器会自动登记 SMB 只读凭据（专用低权限账户，无需手动配置）
6. 游戏本体通过局域网共享运行，需在家庭局域网内的电脑启动；
   外网电脑请在本机安装该游戏——启动器的存档同步与所在网络无关

## 常见问题

**温度/功耗显示离线？**
温度依赖 LibreHardwareMonitor（仅 Windows）。确认 `LibreHardwareMonitor\` 目录存在；
看门狗（MonitorSensorWatch 计划任务）会在数据超时后自动重启采集进程。

**下载机显示未安装？**
配置 `aria2_bin` 指向 aria2c.exe 并重启；aria2 进程由网站自动拉起与守护。

**公网访问怎么做？**
优先 IPv6：路由器开启 IPv6，防火墙放行 18002 端口（`install_admin.bat` 自动配置端口映射），
DDNS 自动更新 AAAA 记录。IPv4 备用：SakuraFrp 建隧道指向本机 8000 端口，把隧道地址填入
`frp_url`，网站会自动检测并在 IPv6 不通时提示切换。

**游戏启动后存档没有同步？**
启动器窗口的完整日志在客户端 `C:\ProgramData\NasGame\launcher.log`；常见原因是
存档路径模板写错或客户端无法访问服务器共享（`games_unc_map` 需包含该游戏所在目录的映射）。

**忘记管理员密码？**
在 `app` 目录执行：
```
python -I -c "import sys; sys.path.insert(0,'.'); import auth; auth.reset_password('admin','新密码')"
```

**改了 config.ini 不生效？**
需要重启服务（`deploy.bat` 或 `重启监控服务.bat`）。注意 `config.ini` 会被网页设置界面
定向回写，手改前建议先备份。

## 更新记录

- **2026-11-08**：SMB 专用只读账户与凭据自动下发（客户端免存管理员密码）；外网启动
  隧道方案验证后退役，改为明确的场景提示；启动器健壮性修复（编码/鉴权/还原时机）
- **2026-11-07**：全站 HTTPS（Let's Encrypt DNS-01 自动签发续期，双协议端口并存）；
  服务端拆分为独立路由模块；影视库剧集合集；分享提取码改 POST 提交；前端公共
  样式/脚本抽取
- **2026-11-06**：文件传输队列与在线编辑（txt/md）、docx/xlsx/pptx 预览；磁盘 SMART
  健康监测；存储迁移后的路径加固
- **2026-11-05**：配置全面迁移至 `config.ini`（统一校验与回写）；新增一键部署脚本与
  本 README；新增 IPv4 备用线路（SakuraFrp）检测/切换与 IPv6 测速仪表盘
- **2026-11-04**：游戏库改为人工策展模式（手动配置上架）；新增多账号五权限位；
  安全加固（任意文件读取/写入修复、密码哈希、上传限额、XSS 修复）
- **2026-11-03**：游戏客户端启动器自更新与调试日志；多扫描根支持
- **2026-11-01/02**：影视库、下载机（aria2）、链接分享、DDNS 自建模块、电源控制、WOL
- **2026-10-29/30**：项目创建，性能监控与文件管理上线

## 许可证

MIT License —— 可自由使用、修改与分发，风险自负。
