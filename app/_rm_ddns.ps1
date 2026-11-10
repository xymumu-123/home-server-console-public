$log = 'D:\Monitor\app\_rm_ddns.log'
$out = @()
$ins = & 'D:\DDNS_GO\ddns-go.exe' -s remove 2>&1 | Out-String
$out += "ddns-go -s remove: " + $ins.Trim()
$svc = Get-Service ddns-go -ErrorAction SilentlyContinue
$out += "service: " + $(if ($svc) { $svc.Status.ToString() } else { "REMOVED" })
$proc = Get-Process ddns-go -ErrorAction SilentlyContinue
if ($proc) { Stop-Process -Name ddns-go -Force; $out += "process: killed" } else { $out += "process: stopped" }
$out | Out-File $log -Encoding utf8
