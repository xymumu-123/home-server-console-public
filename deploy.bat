@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   家庭服务器中控台 - 一键部署 (Windows)
echo ============================================
echo.

rem ---------- 1. 环境检查 ----------
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python。请先安装 Python 3.9 或更高版本,
  echo        安装时勾选 "Add Python to PATH", 然后重新运行本脚本。
  pause & exit /b 1
)
set "PYVER="
for /f "tokens=*" %%i in ('python --version 2^>^&1') do set "PYVER=%%i"
echo %PYVER% | findstr /r "Python" >nul
if errorlevel 1 (
  echo [错误] "python" 不是有效的 Python 安装 ^(可能是 Windows 商店占位程序^),
  echo        请安装正式版 Python 3.9+ 并确保其在 PATH 中位于商店占位之前。
  pause & exit /b 1
)
echo [1/4] 环境检查通过: %PYVER%

rem ---------- 2. 安装依赖 ----------
echo [2/4] 安装 Python 依赖 (fastapi / uvicorn / psutil / requests)...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败, 请检查网络后重试。
  pause & exit /b 1
)

rem ---------- 3. 生成默认配置 ----------
if exist "config.ini" (
  echo [3/4] 检测到已有 config.ini, 保留现有配置。
) else (
  copy config.example.ini config.ini >nul
  echo [3/4] 已从模板生成 config.ini:
  echo       - secret_key / games_token 留空, 首次启动自动生成
  echo       - 首次启动自动创建管理员账号 admin, 初始密码见启动日志
  echo       - 请按需修改 config.ini 后重启 (各配置项含义见 README)
)

rem ---------- 4. 可选组件检查 ----------
if exist "LibreHardwareMonitor\LibreHardwareMonitorLib.dll" (
  echo [可选] 已检测到 LibreHardwareMonitor, 温度/功耗功能可用。
) else (
  echo [可选] 未检测到 LibreHardwareMonitor, 温度/功耗功能将不可用
  echo        ^(不影响网站其余功能; 部署方法见 README 常见问题^)。
)
if exist "aria2\aria2c.exe" (
  echo [可选] 已检测到 aria2, 下载机功能可用。
) else (
  echo [可选] 未检测到 aria2, 下载机功能将不可用 ^(见 README^)。
)

echo [4/4] 启动服务...
echo.
echo   本机访问:   http://127.0.0.1:8000/
echo   局域网访问: http://本机IP:8000/
echo   首次启动的管理员初始密码会打印在下方日志中。
echo   按 Ctrl+C 可停止服务。
echo.
python app\app.py
pause
