$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login' -Method Post -Body @{username='admin';password='0928'} -WebSession $s -UseBasicParsing -TimeoutSec 5 | Out-Null
try {
  $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/current' -WebSession $s -TimeoutSec 5
  Write-Output ("API OK: cpu=" + $r.cpu + " temp=" + $r.cpu_temp + " lhm_ok=" + $r.lhm_ok + " kwh=" + $r.energy_kwh + " disks=" + $r.disks.Count)
} catch { Write-Output "API FAIL: $($_.Exception.Message)" }
try {
  $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/history?minutes=60' -WebSession $s -TimeoutSec 5
  Write-Output ("history rows: " + $h.rows.Count)
} catch { Write-Output "history FAIL: $($_.Exception.Message)" }
# 18002 portproxy test
try {
  $p = Invoke-WebRequest -Uri 'http://localhost:18002/login' -UseBasicParsing -TimeoutSec 5
  Write-Output ("portproxy 18002: " + $p.StatusCode)
} catch { Write-Output "portproxy 18002 FAIL: $($_.Exception.Message)" }
