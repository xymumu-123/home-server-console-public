$log = 'D:\Monitor\app\_restart_web.log'
try {
  Get-Process ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force
  Get-Process aria2c -ErrorAction SilentlyContinue | Stop-Process -Force
  sc.exe delete ddns-go 2>&1 | Out-Null
  schtasks /End /TN MonitorWeb 2>&1 | Out-Null
  Start-Sleep -Seconds 1
  schtasks /Run /TN MonitorWeb 2>&1 | Out-Null
  Start-Sleep -Seconds 4
  $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login' -UseBasicParsing -TimeoutSec 8
  "WEB OK $($r.StatusCode)" | Out-File $log -Encoding utf8
} catch {
  "WEB FAIL: $($_.Exception.Message)" | Out-File $log -Encoding utf8
}
