schtasks /End /TN "MonitorWeb" | Out-Null
schtasks /Run /TN "MonitorWeb" | Out-Null
Start-Sleep -Seconds 6
try {
  $c = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/static/chart.umd.js' -UseBasicParsing -TimeoutSec 5
  Write-Output ("chart.js: " + $c.StatusCode + " (" + $c.RawContentLength + " bytes)")
} catch { Write-Output "chart.js FAIL: $($_.Exception.Message)" }
try {
  $p = Invoke-WebRequest -Uri 'http://localhost:18002/login' -UseBasicParsing -TimeoutSec 5
  Write-Output ("portproxy 18002: " + $p.StatusCode)
} catch { Write-Output "portproxy FAIL" }
