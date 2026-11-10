$log = 'D:\Monitor\app\_mig.log'
$out = @()
$bk = 'D:\Monitor\startup_backup'
New-Item -ItemType Directory -Force -Path $bk | Out-Null
# 1) ddns-go: stop user instance, install as service with existing config
Stop-Process -Name ddns-go -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$ins = & 'D:\DDNS_GO\ddns-go.exe' -s install -c 'C:\Users\Admin\.ddns_go_config.yaml' 2>&1 | Out-String
$out += "ddns-go install output: " + $ins.Trim()
Start-Sleep -Seconds 2
$svc = Get-Service ddns-go -ErrorAction SilentlyContinue
$out += "ddns-go service: " + $(if ($svc) { $svc.Status.ToString() } else { "MISSING" })
# 2) backup + remove old startup entries
Get-ChildItem 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp' -Filter 'alist*' -ErrorAction SilentlyContinue | ForEach-Object {
  Copy-Item $_.FullName $bk -Force; Remove-Item $_.FullName -Force; $out += "removed(common): " + $_.Name }
Get-ChildItem 'C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup' -Filter 'ddns-go*' -ErrorAction SilentlyContinue | ForEach-Object {
  Copy-Item $_.FullName $bk -Force; Remove-Item $_.FullName -Force; $out += "removed(user): " + $_.Name }
# 3) verify ports and processes
Start-Sleep -Seconds 2
$alist = Get-Process alist -ErrorAction SilentlyContinue
$ddns = Get-Process ddns-go -ErrorAction SilentlyContinue
$out += "alist running: " + $(if ($alist) { "YES pid=" + $alist.Id } else { "NO" })
$out += "ddns-go running: " + $(if ($ddns) { "YES pid=" + ($ddns.Id | Select-Object -First 1) } else { "NO" })
$p5244 = Test-NetConnection -ComputerName 127.0.0.1 -Port 5244 -WarningAction SilentlyContinue -InformationLevel Quiet
$p9876 = Test-NetConnection -ComputerName 127.0.0.1 -Port 9876 -WarningAction SilentlyContinue -InformationLevel Quiet
$out += "port 5244 (alist): " + $p5244 + " / port 9876 (ddns-go): " + $p9876
$out | Out-File $log -Encoding utf8
