# 安装 dsh web 引擎开机自启任务 (SYSTEM, onstart) 并立即启动一次
$log = 'D:\Monitor\app\_install_dsh_task.log'
try {
  schtasks /create /tn MonitorDSH /tr "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Monitor\dsh-web.ps1" /sc onstart /ru SYSTEM /rl HIGHEST /f 2>&1 | Out-String | Out-File $log -Encoding utf8
  schtasks /run /tn MonitorDSH 2>&1 | Out-String | Out-File $log -Append -Encoding utf8
  schtasks /query /tn MonitorDSH 2>&1 | Out-String | Out-File $log -Append -Encoding utf8
  "TASK OK" | Out-File $log -Append -Encoding utf8
} catch {
  "TASK FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}
