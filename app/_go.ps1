# stop old app.py instances
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*app.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# stop old daemon instances
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*sensor_daemon*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
# start sensor daemon (hidden, persistent)
Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','D:\Monitor\app\sensor_daemon.ps1' -WindowStyle Hidden
# start web app (hidden, persistent)
Start-Process python -ArgumentList 'D:\Monitor\app\app.py' -WindowStyle Hidden
Start-Sleep -Seconds 6
# login and check API
$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest -Uri 'http://localhost:8000/login' -Method Post -Body @{username='admin';password='0928'} -WebSession $s -UseBasicParsing | Out-Null
$r = Invoke-RestMethod -Uri 'http://localhost:8000/api/current' -WebSession $s -TimeoutSec 5
Write-Output ("cpu={0}% mem={1}% cpu_temp={2}C mb_temp={3}C cpu_power={4}W est={5}W kwh={6} lhm_ok={7} disks={8}" -f $r.cpu,$r.mem,$r.cpu_temp,$r.mb_temp,$r.cpu_power,$r.est_power,$r.energy_kwh,$r.lhm_ok,$r.disks.Count)
