# NasGame 游戏启动器 (由服务器生成, 含设备令牌请勿外传)
$ErrorActionPreference = 'Stop'
$Server = '__SERVER__'
$Token = '__TOKEN__'
$pc = $env:COMPUTERNAME
# 绕过系统代理 (防止代理软件拦截对服务器的请求)
try { [System.Net.WebRequest]::DefaultWebProxy = $null } catch {}

$gameId = $args[0] -replace 'nasgame://','' -replace '/',''

# ---------- 调试日志 (C:\ProgramData\NasGame\launcher.log, 每步留痕, 超 256KB 截断) ----------
$LogDir = "$env:ProgramData\NasGame"
function Log([string]$msg) {
  try {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
    $p = "$LogDir\launcher.log"
    if ((Test-Path $p) -and (Get-Item $p).Length -gt 256KB) { Set-Content -Path $p -Value '' -Encoding UTF8 }
    Add-Content -Path $p -Value ('[' + (Get-Date -Format 'MM-dd HH:mm:ss') + '] ' + $msg) -Encoding UTF8
  } catch {}
}
Log ("=== launcher v10 启动, gameId='$gameId', pc=$pc, args='" + ($args -join ' ') + "'")

if (-not $gameId) {
  Write-Host '用法: launcher.ps1 <游戏ID>'
  Log 'ERROR: gameId 为空 (协议参数未传入或格式异常)'
  Read-Host '按回车关闭'
  exit 1
}
Write-Host 'NasGame 启动器 v10'
$host.UI.RawUI.WindowTitle = 'NasGame 存档同步 - 游戏退出前请勿关闭本窗口'

function Api($method, $path, $body) {
  $path = $path.TrimStart('/')                # 防双斜杠 404
  # PS5.1 两个坑: 无 charset 的 JSON 按 Latin-1 解码 (乱码) → 显式 UTF-8;
  #              GET 带正文会被拒绝 ("无法发送具有此谓词类型的内容正文") → 仅写方法带正文
  if ($method -eq 'Get') {
    $resp = Invoke-WebRequest -Uri "$Server/api/games/$path" -Method Get `
      -Headers @{ 'X-Game-Token' = $Token } -TimeoutSec 30 -UseBasicParsing
  } else {
    $resp = Invoke-WebRequest -Uri "$Server/api/games/$path" -Method $method `
      -Headers @{ 'X-Game-Token' = $Token } -ContentType 'application/json' `
      -Body ($(if ($body) { $body | ConvertTo-Json -Depth 4 } else { 'null' })) -TimeoutSec 30 -UseBasicParsing
  }
  if ($resp.RawContentStream -and $resp.RawContentStream.Length -gt 0) {
    return ([Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()) | ConvertFrom-Json)
  }
}

# ---------- 外网启动隧道 (v10): 外网时把域名钉到 127.0.0.2, 经本机转发到服务器:18001 ----------
# 背景: Windows SMB 客户端只拨 445 且 UNC 无端口语法; 服务器 18001 对外开放 (18001→445)。
# 机制: hosts 钉域名 → 127.0.0.2 (本机回环) → portproxy 127.0.0.2:445 → 服务器:18001;
#       网页/API 同理走 127.0.0.2:18002 → 服务器:18002。需要停用本机 Server 服务腾出 445,
#       仅外网启动时临时执行, 游戏退出后还原 (高权限计划任务, setup 后每次零 UAC)。
$Domain = ([Uri]$Server).Host
$HostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
$TunnelPs1 = "$env:ProgramData\NasGame\tunnel.ps1"

function TcpTest([string]$h, [int]$p, [int]$ms = 2500) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $ar = $c.BeginConnect($h, $p, $null, $null)
    if ($ar.AsyncWaitHandle.WaitOne($ms) -and $c.Connected) { $c.Close(); return $true }
    $c.Close(); return $false
  } catch { try { $c.Close() } catch {}; return $false }
}

function HostsPinned {
  # 匹配新旧两种钉法: 127.0.0.2 <裸域名> (旧) 与 127.0.0.2 smb.<域名> (现)
  try { return [bool](Select-String -Path $HostsPath -Pattern ('^\s*127\.0\.0\.2\s+.*' + [regex]::Escape($Domain)) -Quiet) } catch { return $false }
}

function TasksRegistered {
  # 用 cmdlet 而非 schtasks.exe: 原生命令的 stderr 在 EAP=Stop 下会变成终止错误
  return [bool](Get-ScheduledTask -TaskName NasGameTunnelOn -ErrorAction SilentlyContinue)
}

function Write-TunnelFiles {
  if (-not (Test-Path "$env:ProgramData\NasGame")) { New-Item -ItemType Directory -Force -Path "$env:ProgramData\NasGame" | Out-Null }
  $t = @'
param([string]$mode = 'on')
$ErrorActionPreference = 'Continue'
$domain = '__DOMAIN__'
$smbPort = 18001
$webPort = 18002
$pin = '127.0.0.2'
$hostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
$logPath = "$env:ProgramData\NasGame\tunnel.log"
$stateFile = "$env:ProgramData\NasGame\tunnel_state.txt"
function TLog([string]$m) {
  try { Add-Content -Path $logPath -Value ('[' + (Get-Date -Format 'MM-dd HH:mm:ss') + '] ' + $m) -Encoding UTF8 } catch {}
}
TLog "=== tunnel $mode ==="
if ($mode -eq 'on') {
  $ip = $null
  try { $ip = (Resolve-DnsName -Name $domain -Type AAAA -ErrorAction Stop | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress } catch {}
  if (-not $ip) { try { $ip = (Resolve-DnsName -Name $domain -Type A -ErrorAction Stop | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress } catch {} }
  if (-not $ip) { TLog "ERROR: 无法解析 $domain"; exit 1 }
  TLog "server ip: $ip"
  # 记录并停用本机共享服务 (释放 445; 只在首次记录原启动类型)
  $svc = Get-Service LanmanServer
  if (-not (Test-Path $stateFile)) { [string]$svc.StartType | Set-Content $stateFile }
  if ($svc.Status -ne 'Stopped') { Stop-Service LanmanServer -Force }
  Set-Service LanmanServer -StartupType Disabled
  # hosts 钉 smb 子域 (清掉本脚本名下所有 127.0.0.2 行, 含旧版裸域名单);
  # 只钉 smb.* 而非主域名: 网页/API 走真实 DNS, 隧道任何状态下都不可能连坐。
  # 写法用 Clear-Content + Add-Content 分步落盘 (管道一次性写回同文件存在静默失败案例)
  $keep = (Get-Content $hostsPath) | Where-Object { $_ -notmatch ('^\s*' + [regex]::Escape($pin) + '\s') }
  Clear-Content $hostsPath
  $keep | Add-Content -Path $hostsPath -Encoding ASCII
  Add-Content -Path $hostsPath -Value ("$pin`tsmb.$domain") -Encoding ASCII
  # 转发规则 (先删再加, 幂等); 只需 445 一条 (网页不走隧道)
  netsh interface portproxy delete v4tov6 listenaddress=$pin listenport=445 2>$null | Out-Null
  netsh interface portproxy add v4tov6 listenaddress=$pin listenport=445 connectaddress=$ip connectport=$smbPort | Out-Null
  netsh interface portproxy delete v4tov6 listenaddress=$pin listenport=$webPort 2>$null | Out-Null
  ipconfig /flushdns | Out-Null
  Start-Sleep -Milliseconds 500
  TLog "tunnel on 完成"
} else {
  (Get-Content $hostsPath) | Where-Object { $_ -notmatch ('^\s*' + [regex]::Escape($pin) + '\s') } | Set-Content $hostsPath -Encoding ASCII
  netsh interface portproxy delete v4tov6 listenaddress=$pin listenport=445 2>$null | Out-Null
  netsh interface portproxy delete v4tov6 listenaddress=$pin listenport=$webPort 2>$null | Out-Null
  $orig = 'Automatic'
  if (Test-Path $stateFile) { $orig = (Get-Content $stateFile -Raw).Trim(); Remove-Item $stateFile -Force }
  Set-Service LanmanServer -StartupType $orig
  if ($orig -ne 'Disabled') { Start-Service LanmanServer }
  ipconfig /flushdns | Out-Null
  TLog "tunnel off 完成"
}
'@
  Set-Content -Path $TunnelPs1 -Value ($t.Replace('__DOMAIN__', $Domain)) -Encoding UTF8
  $r = @'
$ps = "$env:ProgramData\NasGame\tunnel.ps1"
schtasks /Create /TN NasGameTunnelOn  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps`" -mode on"  /SC ONCE /ST 00:00 /RU SYSTEM /RL HIGHEST /F | Out-Null
schtasks /Create /TN NasGameTunnelOff /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps`" -mode off" /SC ONCE /ST 00:00 /RU SYSTEM /RL HIGHEST /F | Out-Null
'@
  Set-Content -Path "$env:ProgramData\NasGame\tunnel_register.ps1" -Value $r -Encoding UTF8
}

function Ensure-TunnelRegistered {
  # 每次都重写隧道脚本: 启动器自更新后, 已注册任务的老机器也能拿到新逻辑
  # (任务只需注册一次, 脚本内容随启动器版本走)
  Write-TunnelFiles
  if (-not (TasksRegistered)) {
    Write-Host '首次外网启动: 需要一次性管理员授权 (UAC 确认后不再弹出)…'
    try {
      Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$env:ProgramData\NasGame\tunnel_register.ps1"
    } catch { return $false }
  }
  return (TasksRegistered)
}

function Run-Tunnel([string]$mode) {
  Start-ScheduledTask -TaskName ("NasGameTunnel" + $mode) -ErrorAction SilentlyContinue
  return $true
}

# 自愈: 上次会话异常退出会留下 hosts 钉住/服务停用, 启动时先洗干净
if (HostsPinned) {
  Log '检测到隧道残留 (hosts 未还原), 执行清理'
  Write-Host '清理上次外网隧道残留…'
  if (-not (TasksRegistered)) { [void](Ensure-TunnelRegistered) }
  if (TasksRegistered) { [void](Run-Tunnel 'Off'); Start-Sleep -Seconds 3 }
  elseif (HostsPinned) {
    try { Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',"& '$TunnelPs1' -mode off" } catch {}
  }
  Log ('隧道自愈完成, 仍钉住: ' + (HostsPinned))
}

# 自更新: 服务器端模板更新后自动同步 (下载失败继续用本地版本)
try {
  $tmp = "$env:TEMP\nasgame_launcher_new.ps1"
  Invoke-RestMethod -Uri "$Server/api/games/client/ps1?token=$Token" -OutFile $tmp -TimeoutSec 15
  if ((Get-FileHash $tmp -Algorithm SHA256).Hash -ne (Get-FileHash $PSCommandPath -Algorithm SHA256).Hash) {
    Copy-Item $tmp $PSCommandPath -Force
    Write-Host '启动器已自动更新, 重新运行...'
    & $PSCommandPath @args
    exit
  }
} catch {}

# ---------- 加载提示弹窗 (独立 STA 线程; 主线程置 $sync.Stop 后弹窗自行关闭, 不跨线程碰控件) ----------
$sync = [hashtable]::Synchronized(@{})
try { Add-Type -AssemblyName System.Windows.Forms; $sync.FormsOk = $true } catch {}

function Show-Busy([string]$text) {
  try {
    if (-not $sync.FormsOk) { return }
    $sync.Text = $text
    $sync.Stop = $false
    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = 'STA'
    $rs.Open()
    $psi = [powershell]::Create()
    $psi.Runspace = $rs
    [void]$psi.AddScript({
      param($s)                                # 同步表必须显式传入 (runspace 看不到主线程变量)
      Add-Type -AssemblyName System.Windows.Forms
      Add-Type -AssemblyName System.Drawing
      $f = New-Object System.Windows.Forms.Form
      $f.Text = 'NasGame'
      $f.Size = New-Object System.Drawing.Size(380, 140)
      $f.StartPosition = 'CenterScreen'
      $f.FormBorderStyle = 'FixedDialog'
      $f.TopMost = $true
      $f.ControlBox = $false
      $f.ShowInTaskbar = $false
      $l = New-Object System.Windows.Forms.Label
      $l.Dock = 'Fill'; $l.TextAlign = 'MiddleCenter'
      $l.Font = New-Object System.Drawing.Font('Microsoft YaHei', 10)
      $pb = New-Object System.Windows.Forms.ProgressBar
      $pb.Dock = 'Bottom'; $pb.Height = 22
      $pb.Style = 'Marquee'; $pb.MarqueeAnimationSpeed = 25
      $f.Controls.Add($l)
      $f.Controls.Add($pb)
      $t = New-Object System.Windows.Forms.Timer
      $t.Interval = 150
      $t.Add_Tick({ $l.Text = $s.Text; if ($s.Stop) { $f.Close() } })
      $t.Start()
      [void]$f.ShowDialog()
    }).AddArgument($sync)
    $sync.PS = $psi
    $sync.Handle = $psi.BeginInvoke()
  } catch { }
}

function Hide-Busy {
  try {
    if ($sync.PS) {
      $sync.Stop = $true
      # 最多等 2 秒让弹窗自行关闭, 超时强停 (绝不能因弹窗卡住启动流程)
      $deadline = (Get-Date).AddSeconds(2)
      while ((Get-Date) -lt $deadline -and
             $sync.PS.InvocationStateInfo.State -eq [System.Management.Automation.PSInvocationState]::Running) {
        Start-Sleep -Milliseconds 100
      }
      if ($sync.PS.InvocationStateInfo.State -eq [System.Management.Automation.PSInvocationState]::Running) {
        try { $sync.PS.Stop() } catch {}
      }
      try { $sync.PS.EndInvoke($sync.Handle) } catch {}
      try { $sync.PS.Runspace.Close() } catch {}
      $sync.PS = $null
    }
  } catch { }
}

try {
  # 租约检查 (含存档路径模板)
  $lease = Api Post '/launch' @{ game = $gameId; pc = $pc; force = $false }
  Log ("lease ok: exe='" + $lease.exe + "' tpl='" + $lease.save_tpl + "' conflict=" + $lease.conflict)
  if ($lease.conflict) {
    $ans = Read-Host "WARNING: 游戏[$gameId]正在电脑[$($lease.holder)]上运行(自 $($lease.since))。仍要启动并覆盖存档? (y/n)"
    if ($ans -ne 'y') { Write-Host '已取消'; Log '用户取消 (租约冲突)'; Read-Host '按回车关闭'; exit }
    $lease = Api Post '/launch' @{ game = $gameId; pc = $pc; force = $true }
    Log 'lease force 重取成功'
  }
  if (-not $lease.exe) { Write-Host '服务器上该游戏未确认启动程序'; Log 'lease.exe 为空, 退出'; Read-Host '按回车关闭'; exit 1 }
  Write-Host "游戏: $($lease.name)"

  # 存档目录: 服务器模板展开 → 本机已学习 → 手动输入兜底
  # 模板支持 Windows 原生变量 (%USERPROFILE%/%APPDATA%/%LOCALAPPDATA% 等,
  # 永远指向本机真实目录) 与旧式 {user}/{pc} 兼容
  $marker = "$env:ProgramData\NasGame\saves.json"
  $dirs = @{}
  if (Test-Path $marker) { $dirs = Get-Content $marker -Raw | ConvertFrom-Json }
  $saveDir = $null
  if ($lease.save_tpl) {
    $tpl = [string]$lease.save_tpl
    # 未知变量检测: 展开后指向会错, 直接判失败走手动兜底
    $unknown = @([regex]::Matches($tpl, '%([A-Za-z0-9_]+)%') |
      Where-Object { [Environment]::GetEnvironmentVariable($_.Groups[1].Value) -eq $null } |
      ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
    if ($unknown.Count) {
      Write-Host "警告: 存档路径含本机不存在的变量 ($($unknown -join ', ')), 跳过模板"
    } else {
      # 原生展开: %VAR% → 本机环境变量值
      $saveDir = [regex]::Replace($tpl, '%([A-Za-z0-9_]+)%', {
        param($m) [Environment]::GetEnvironmentVariable($m.Groups[1].Value)
      })
      # 旧式变量兼容
      $saveDir = $saveDir.Replace('{user}', $env:USERNAME).Replace('{pc}', $pc)
      Write-Host "存档目录 (服务器配置): $saveDir"
    }
  }
  if (-not $saveDir -and $dirs.$gameId) {
    $saveDir = $dirs.$gameId
    Write-Host "存档目录 (本机已学习): $saveDir"
  }
  if (-not $saveDir) {
    $saveDir = Read-Host '未配置存档路径, 请输入本机该游戏的存档目录 (直接回车=跳过存档同步)'
  }
  Log ("saveDir='" + $saveDir + "'")
  if ($saveDir) {
    if ($dirs.$gameId -ne $saveDir) {
      $dirs | Add-Member -NotePropertyName $gameId -NotePropertyValue $saveDir -Force
      New-Item -ItemType Directory -Force -Path "$env:ProgramData\NasGame" | Out-Null
      $dirs | ConvertTo-Json | Set-Content $marker -Encoding UTF8
      try { Api Post '/save/dir' @{ game = $gameId; pc = $pc; dir = $saveDir } | Out-Null }
      catch { Write-Host "提示: 存档目录登记失败(不影响游玩): $($_.Exception.Message)" }
    }
    if (-not (Test-Path $saveDir)) { New-Item -ItemType Directory -Force -Path $saveDir | Out-Null }
  }

  # 弹出加载提示 (覆盖 同步存档→启动→游戏窗口出现 的整个静默期)
  Show-Busy '正在准备游戏 (同步存档)…'

  # 拉取最新快照
  if ($saveDir) {
    try {
      Invoke-RestMethod -Uri "$Server/api/games/save/pull?game=$gameId&token=$Token" `
        -OutFile "$env:TEMP\nasgame_pull.zip" -TimeoutSec 300
      if (Test-Path "$env:TEMP\nasgame_pull.zip") {
        Expand-Archive -Path "$env:TEMP\nasgame_pull.zip" -DestinationPath $saveDir -Force
        Remove-Item "$env:TEMP\nasgame_pull.zip" -Force
        Write-Host '存档已同步到本机'
      }
    } catch { Write-Host '暂无历史快照, 跳过同步' }
  }

  # 启动游戏 (UNC 路径直接启动); 游戏窗口出现后加载提示自动消失
  # 启动游戏: 不做网络预判 (双宿主/代理机器上预探测会假阴性), 失败时在 catch 里给出原因与指引
  $sync.Text = '正在启动游戏, 请稍候…'
  $exeDir = Split-Path $lease.exe
  Write-Host "启动: $($lease.exe)"
  try {
    $proc = Start-Process -FilePath $lease.exe -WorkingDirectory $exeDir -PassThru -ErrorAction Stop
    Log ("Start-Process pid=" + $proc.Id)
  } catch {
    Log ("Start-Process 失败: " + $_.Exception.Message)
    Hide-Busy
    Write-Host ''
    Write-Host '=== 无法启动共享盘上的游戏 ===' -ForegroundColor Yellow
    Write-Host ("原因: " + $_.Exception.Message)
    Write-Host '常见情形: 正在外网 (共享盘游戏跨公网无法运行), 或服务器/共享异常。'
    Write-Host '外网游玩: 在本机安装该游戏 (启动器会自动同步它的存档); 回到家庭局域网即可直接启动。'
    try { Api Post '/launch/end' @{ game = $gameId; pc = $pc } | Out-Null; Log '已释放租约' } catch {}
    Read-Host '按回车关闭'
    exit 1
  }
  # 轮询等待游戏窗口出现 (局域网 90 秒; 外网隧道跨公网拉起慢, 给 240 秒)
  $waitSecs = 90
  $deadline = (Get-Date).AddSeconds($waitSecs)
  while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) { break }
    $proc.Refresh()
    if ($proc.MainWindowHandle -ne 0) { break }
    Start-Sleep -Milliseconds 500
  }
  Hide-Busy
  Log ("窗口等待结束: procHasExited=" + $proc.HasExited)
  if ($proc.HasExited) {
    # 子进程秒退: 多为"中转启动器"型游戏, 找同目录新出现的真游戏进程接着等
    Start-Sleep -Seconds 2
    $real = Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -and $_.Path.StartsWith($exeDir, [StringComparison]::OrdinalIgnoreCase) } |
      Sort-Object StartTime -Descending | Select-Object -First 1
    if ($real) {
      Write-Host "检测到游戏主进程: $($real.ProcessName)"
      $proc = $real
    } else {
      Write-Host '游戏进程已退出 (可能是中转启动器)'
    }
  }
  Write-Host ''
  Write-Host '=== 游戏运行中: 请勿关闭本窗口! (关闭将导致存档无法回写) ===' -ForegroundColor Yellow
  Write-Host ''
  try { Wait-Process -Id $proc.Id -ErrorAction Stop } catch {}
  Write-Host '游戏已退出, 开始回写存档…'

  # 回传存档
  if ($saveDir -and (Test-Path $saveDir)) {
    $files = Get-ChildItem $saveDir -Recurse -File -ErrorAction SilentlyContinue
    if ($files) {
      $zip = "$env:TEMP\nasgame_push.zip"
      if (Test-Path $zip) { Remove-Item $zip -Force }
      try {
        Compress-Archive -Path "$saveDir\*" -DestinationPath $zip -Force
        Invoke-RestMethod -Uri "$Server/api/games/save/push?game=$gameId&pc=$pc&token=$Token" `
          -Method Post -ContentType 'application/zip' -InFile $zip -TimeoutSec 600
        Write-Host '存档已回传服务器'
        Log '存档回传成功'
      } catch { Write-Host "存档回传失败: $_"; Log ("存档回传失败: " + $_.Exception.Message) }
    }
  }
  try { Api Post '/launch/end' @{ game = $gameId; pc = $pc } | Out-Null; Log 'launch/end 已释放租约' } catch {}
  Write-Host '完成, 本窗口可以关闭'
  Log '=== 完成'
  Start-Sleep -Seconds 3
} catch {
  Hide-Busy
  Log ("ERROR: " + $_.Exception.Message)
  Log ($_.ScriptStackTrace)
  # 启动失败也释放租约, 避免服务器上一直显示"游戏中"
  try { Api Post '/launch/end' @{ game = $gameId; pc = $pc } | Out-Null; Log '已释放租约' } catch {}

  Write-Host ""
  Write-Host ("发生错误: " + $_.Exception.Message)
  Write-Host ("详细日志: $LogDir\launcher.log")
  Read-Host '按回车关闭'
}
