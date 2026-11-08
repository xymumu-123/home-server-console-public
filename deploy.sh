#!/usr/bin/env bash
# 家庭服务器中控台 - 一键部署 (Linux/Mac)
# 用法: bash deploy.sh
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  家庭服务器中控台 - 一键部署 (Linux/Mac)"
echo "============================================"

# ---------- 1. 环境检查 ----------
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3, 请先安装 Python 3.9+ (apt install python3 python3-pip)"
  exit 1
fi
echo "[1/4] 环境检查通过: $(python3 --version)"

# ---------- 2. 安装依赖 ----------
echo "[2/4] 安装 Python 依赖 (fastapi / uvicorn / psutil / requests)..."
python3 -m pip install -r requirements.txt || {
  echo "[提示] 直接安装失败, 尝试使用 --break-system-packages (Debian 12+/Ubuntu 23+)..."
  python3 -m pip install --break-system-packages -r requirements.txt
}

# ---------- 3. 生成默认配置 ----------
if [ -f config.ini ]; then
  echo "[3/4] 检测到已有 config.ini, 保留现有配置。"
else
  cp config.example.ini config.ini
  echo "[3/4] 已从模板生成 config.ini:"
  echo "      - secret_key / games_token 留空, 首次启动自动生成"
  echo "      - 首次启动自动创建管理员 admin, 初始密码见启动日志"
fi

# ---------- 4. 可选组件检查 ----------
[ -f "LibreHardwareMonitor/LibreHardwareMonitorLib.dll" ] \
  && echo "[可选] 已检测到 LibreHardwareMonitor (注意: 该组件仅限 Windows)" \
  || echo "[可选] 未检测到 LibreHardwareMonitor, 温度/功耗功能不可用 (不影响其余功能)"
command -v ffmpeg >/dev/null 2>&1 \
  && echo "[可选] 已检测到 ffmpeg, 缩略图/转码可用" \
  || echo "[可选] 未检测到 ffmpeg, 缩略图/转码不可用 (apt install ffmpeg)"

echo "[4/4] 启动服务..."
echo
echo "  本机访问:   http://127.0.0.1:8000/"
echo "  局域网访问: http://本机IP:8000/"
echo "  首次启动的管理员初始密码会打印在下方日志中。"
echo "  Ctrl+C 停止服务。"
echo
exec python3 app/app.py
