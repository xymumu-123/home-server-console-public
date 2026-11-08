@echo off
echo === NasGame 游戏库接入 (免管理员, 注册到当前用户) ===
set "DIR=%ProgramData%\NasGame"
if not exist "%DIR%" mkdir "%DIR%"
echo 取回启动器...
powershell -NoProfile -Command "Invoke-RestMethod -Uri '__SERVER__/api/games/client/ps1?token=__TOKEN__' -OutFile '%DIR%\launcher.ps1'"
if exist "%DIR%\launcher.ps1" goto dl_ok
echo PowerShell 取回失败, 用 curl 直连重试...
curl -s --noproxy "*" -o "%DIR%\launcher.ps1" "__SERVER__/api/games/client/ps1?token=__TOKEN__"
:dl_ok
if not exist "%DIR%\launcher.ps1" echo 启动器下载失败 & pause & exit /b 1
echo 注册 nasgame:// 协议...
reg add "HKCU\Software\Classes\nasgame" /ve /d "url:NasGame Game" /f >nul
reg add "HKCU\Software\Classes\nasgame" /v "URL Protocol" /f >nul
reg add "HKCU\Software\Classes\nasgame\shell\open\command" /ve /d "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\NasGame\launcher.ps1 %%1" /f >nul
echo.
echo === 接入完成! 以后在中控台点"启动"即可 ===
pause
