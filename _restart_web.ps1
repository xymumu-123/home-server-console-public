$log = 'D:\Monitor\app\_restart_web.log'
try {
  Get-Process ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force
  Get-Process aria2c -ErrorAction SilentlyContinue | Stop-Process -Force
  sc.exe delete ddns-go 2>&1 | Out-Null
  # 显式杀掉 app.py 进程 (schtasks /End 杀不死 detached 的 SYSTEM python)
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*app.py*' } | ForEach-Object {
      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }
  # 传感器守护一并重启 (LHM 可能处于挂死状态)
  Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*sensor_daemon*' } | ForEach-Object {
      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
  schtasks /End /TN MonitorSensors 2>&1 | Out-Null
  schtasks /Run /TN MonitorSensors 2>&1 | Out-Null
  # dsh web 引擎一并拉起 (dsh-web.ps1 自带单实例守卫, 已在跑则跳过)
  Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','D:\Monitor\dsh-web.ps1'
  schtasks /End /TN MonitorWeb 2>&1 | Out-Null
  Start-Sleep -Seconds 2
  schtasks /Run /TN MonitorWeb 2>&1 | Out-Null
  Start-Sleep -Seconds 4
  $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login' -UseBasicParsing -TimeoutSec 8
  "WEB OK $($r.StatusCode)" | Out-File $log -Encoding utf8
} catch {
  "WEB FAIL: $($_.Exception.Message)" | Out-File $log -Encoding utf8
}
