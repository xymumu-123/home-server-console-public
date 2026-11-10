$ErrorActionPreference = 'Continue'
$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest -Uri 'http://127.0.0.1:8000/login' -Method Post -Body @{username='admin';password='0928'} -WebSession $s -UseBasicParsing -TimeoutSec 5 | Out-Null

# 1. roots
try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/fs/roots' -WebSession $s -TimeoutSec 5
  Write-Output ("roots: " + (($r | ForEach-Object { $_.path }) -join ', ')) } catch { Write-Output "roots FAIL: $($_.Exception.Message)" }

# 2. list D:\
try { $r = Invoke-RestMethod -Uri ('http://127.0.0.1:8000/api/fs/list?path=' + [uri]::EscapeDataString('D:\')) -WebSession $s -TimeoutSec 5
  Write-Output ("list D:\ : dirs=" + $r.dirs.Count + " files=" + $r.files.Count) } catch { Write-Output "list FAIL: $($_.Exception.Message)" }

# 3. path traversal protection
try {
  Invoke-RestMethod -Uri ('http://127.0.0.1:8000/api/fs/list?path=' + [uri]::EscapeDataString('\\..\..\..')) -WebSession $s -TimeoutSec 5 | Out-Null
  Write-Output "TRAVERSAL FAIL: allowed!"
} catch { Write-Output ("traversal blocked: " + $_.Exception.Response.StatusCode.value__) }

# 4. mkdir + rename + upload + delete roundtrip in D:\Monitor\tmp_test
$null = New-Item -ItemType Directory -Force -Path 'D:\Monitor\tmp_test'
try {
  Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/fs/mkdir' -Method Post -Headers @{accept='application/json'} -ContentType 'application/json' -Body '{"path":"D:\\Monitor\\tmp_test","name":"sub"}' -WebSession $s -TimeoutSec 5 | Out-Null
  Write-Output "mkdir OK"
} catch { Write-Output "mkdir FAIL: $($_.Exception.Message)" }

$bytes = [System.Text.Encoding]::UTF8.GetBytes("hello from upload test")
try {
  Invoke-RestMethod -Uri ('http://127.0.0.1:8000/api/fs/upload?path=' + [uri]::EscapeDataString('D:\Monitor\tmp_test') + '&name=test.txt') -Method Put -Body $bytes -WebSession $s -ContentType 'application/octet-stream' -TimeoutSec 10 | Out-Null
  $content = Get-Content 'D:\Monitor\tmp_test\test.txt' -Raw
  Write-Output ("upload OK, content=" + $content.Trim())
} catch { Write-Output "upload FAIL: $($_.Exception.Message)" }

# 5. range stream on uploaded file
try {
  $req = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8000/api/fs/stream?path=' + [uri]::EscapeDataString('D:\Monitor\tmp_test\test.txt'))
  $req.AddRange(0, 4); $req.CookieContainer = New-Object System.Net.CookieContainer
  $ck = New-Object System.Net.Cookie('sid', ($s.Cookies.GetCookies('http://127.0.0.1:8000/login')[0].Value), '/', '127.0.0.1')
  $req.CookieContainer.Add($ck)
  $resp = $req.GetResponse()
  Write-Output ("range OK: status=" + [int]$resp.StatusCode + " cr=" + $resp.Headers['Content-Range'])
  $resp.Close()
} catch { Write-Output "range FAIL: $($_.Exception.Message)" }

# 6. delete
try {
  Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/fs/delete' -Method Post -ContentType 'application/json' -Body '{"path":"D:\\Monitor\\tmp_test"}' -WebSession $s -TimeoutSec 5 | Out-Null
  if (Test-Path 'D:\Monitor\tmp_test') { Write-Output "delete FAIL: still exists" } else { Write-Output "delete OK (recursive)" }
} catch { Write-Output "delete FAIL: $($_.Exception.Message)" }
