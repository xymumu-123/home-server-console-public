$log = 'D:\Monitor\app\_audit_fix.log'
$out = @()
# 1. Re-add missing portproxy 18002 -> 8000
netsh interface portproxy add v6tov4 listenport=18002 listenaddress="::" connectaddress="127.0.0.1" connectport=8000 | Out-Null
$pp = netsh interface portproxy show all | Select-String "18002"
$out += "portproxy 18002: " + $(if ($pp) { "OK" } else { "FAIL" })
# 2. Firewall rules (idempotent rebuild)
netsh advfirewall firewall delete rule name="Monitor18002" 2>&1 | Out-Null
netsh advfirewall firewall add rule name="Monitor18002" dir=in action=allow protocol=TCP localport=18002 | Out-Null
netsh advfirewall firewall delete rule name="Monitor8000-LAN" 2>&1 | Out-Null
netsh advfirewall firewall add rule name="Monitor8000-LAN" dir=in action=allow protocol=TCP localport=8000 remoteip=localsubnet | Out-Null
$fw = netsh advfirewall firewall show rule name="Monitor18002" | Select-String "Enabled|LocalPort"
$out += "firewall Monitor18002: " + ($fw | Out-String).Trim()
# 3. Alist autostart task (boot, SYSTEM)
Set-Content -Path 'D:\alist\start_alist.bat' -Value "@echo off`r`ncd /d D:\alist`r`nalist.exe server" -Encoding ascii
schtasks /Create /F /TN "MonitorAlist" /TR "\"D:\alist\start_alist.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST 2>&1 | Out-Null
$at = schtasks /query /tn MonitorAlist 2>&1 | Out-String
$out += "MonitorAlist task: " + $(if ($at -match "MonitorAlist") { "OK" } else { "FAIL: " + $at.Trim() })
# 4. Monitor task states
foreach ($tn in @("MonitorWeb", "MonitorSensors")) {
    $st = schtasks /query /tn $tn /fo LIST 2>&1 | Select-String ":" | Select-Object -First 3
    $out += "$tn registered: " + $(if ($st) { "OK" } else { "FAIL" })
}
$out | Out-File $log -Encoding utf8
