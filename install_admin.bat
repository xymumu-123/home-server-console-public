@echo off
REM ============================================
REM  Server Monitor - one-time admin installer
REM  Creates: scheduled tasks (autostart as SYSTEM),
REM           IPv6 port proxy 18002->8000, firewall rules
REM ============================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/5] Stopping manual monitor instances...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like '*sensor_daemon*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

echo [2/5] Creating scheduled task: MonitorSensors (sensor daemon)...
schtasks /Create /F /TN "MonitorSensors" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\Monitor\app\sensor_daemon.ps1" /SC ONSTART /RU SYSTEM /RL HIGHEST

echo [3/5] Creating scheduled task: MonitorWeb (web server)...
schtasks /Create /F /TN "MonitorWeb" /TR "\"C:\Users\Admin\AppData\Local\Programs\Python\Python39\python.exe\" D:\Monitor\app\app.py" /SC ONSTART /RU SYSTEM /RL HIGHEST

echo [4/5] Adding IPv6 port proxy (18002 -^> 8000) and firewall rules...
netsh interface portproxy add v6tov4 listenport=18002 listenaddress=:: connectaddress=127.0.0.1 connectport=8000
netsh advfirewall firewall delete rule name="Monitor18002" >nul 2>&1
netsh advfirewall firewall add rule name="Monitor18002" dir=in action=allow protocol=TCP localport=18002
netsh advfirewall firewall delete rule name="Monitor8000-LAN" >nul 2>&1
netsh advfirewall firewall add rule name="Monitor8000-LAN" dir=in action=allow protocol=TCP localport=8000 remoteip=localsubnet

echo [5/5] Starting services now...
schtasks /Run /TN "MonitorSensors"
schtasks /Run /TN "MonitorWeb"
timeout /t 8 /nobreak >nul

echo.
echo ===== Verify =====
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login' -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 'FAIL: ' + $_.Exception.Message }"
netsh interface portproxy show all | findstr 18002
echo.
echo ===== INSTALL DONE =====
echo Web: http://localhost:8000  (LAN: http://<this-ip>:8000, IPv6: port 18002)
pause
