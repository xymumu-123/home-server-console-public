# 一次性注册传感器看门狗计划任务 (需管理员)
$log = 'D:\Monitor\app\_install_watchdog.log'
try {
    schtasks /Create /F /TN "MonitorSensorWatch" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\Monitor\app\sensor_watchdog.ps1" /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST
    schtasks /Run /TN "MonitorSensorWatch"
    "watchdog task registered + first run triggered" | Out-File $log -Encoding utf8
} catch {
    $_.Exception.Message | Out-File $log -Encoding utf8
}
