@echo off
chcp 65001 >nul
echo 正在请求管理员权限并重启监控站（请留意 UAC 弹窗，点"是"）...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File D:\Monitor\app\_restart_web.ps1'"
echo 已提交提权请求。结果将显示在 D:\Monitor\app\_restart_web.log
timeout /t 12 /nobreak >nul
type D:\Monitor\app\_restart_web.log
echo.
echo 本窗口可以关闭。
timeout /t 15 /nobreak >nul
