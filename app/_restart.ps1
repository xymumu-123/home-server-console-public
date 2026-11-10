Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*app.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
Start-Process python -ArgumentList 'D:\Monitor\app\app.py' -WindowStyle Hidden `
  -RedirectStandardOutput 'D:\Monitor\app\run.log' -RedirectStandardError 'D:\Monitor\app\run.err'
Write-Output "restarted"
