# sensor_watchdog.ps1 - 传感器看门狗 (计划任务每 5 分钟调用, SYSTEM)
# sensors.json 超过 90 秒未更新 = sensor_daemon 挂死 (LHM Update 无超时会死锁) → 强杀并重启
$log = 'D:\Monitor\app\sensor_watchdog.log'
$outFile = 'D:\Monitor\sensors.json'
$ageSec = 99999
try {
    $ageSec = [int]((Get-Date) - (Get-Item $outFile).LastWriteTime).TotalSeconds
} catch { }

if ($ageSec -gt 90) {
    $line = '[' + (Get-Date -Format 'MM-dd HH:mm:ss') + '] 传感器数据超龄 ' + $ageSec + 's, 重启 sensor_daemon'
    Add-Content -Path $log -Value $line -Encoding UTF8
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like '*sensor_daemon*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    schtasks /End /TN MonitorSensors 2>&1 | Out-Null
    Start-Sleep -Seconds 1
    schtasks /Run /TN MonitorSensors 2>&1 | Out-Null
}
