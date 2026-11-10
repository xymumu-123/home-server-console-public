$log = 'D:\Monitor\app\_rm_svc.log'
$out = @()
$stop = sc.exe stop ddns-go 2>&1 | Out-String
Start-Sleep -Seconds 2
$del = sc.exe delete ddns-go 2>&1 | Out-String
$out += "sc delete: " + $del.Trim()
$exists = Get-Service ddns-go -ErrorAction SilentlyContinue
$out += "service after: " + $(if ($exists) { "STILL REGISTERED (" + $exists.Status + ")" } else { "GONE" })
$out | Out-File $log -Encoding utf8
