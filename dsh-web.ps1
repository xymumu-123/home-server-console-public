# dsh-web.ps1 - dsh (DeepSeek Harness) Web 引擎常驻启动器
# 用法: powershell -ExecutionPolicy Bypass -File D:\Monitor\dsh-web.ps1
# 职责: 读 config.ini [ai] ai_api_key -> DEEPSEEK_API_KEY; 拉起 dsh web (仅回环 3080)。
#       控制台 /ai 反代 (app/ai_routes.py) 依赖本引擎在线。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'dsh-web.log'

function Log($msg) {
    $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# ---- 读 config.ini [ai] 段 (PS5.1 无 Ini 解析, 手工逐行) ----
$apiKey = ''
$dshBin = ''
$section = ''
try {
    Get-Content (Join-Path $root 'config.ini') -Encoding UTF8 | ForEach-Object {
        $t = $_.Trim()
        if ($t -match '^\[(.+)\]$') { $section = $Matches[1].ToLower() }
        elseif ($section -eq 'ai' -and $t -match '^([^=;#]+)=(.*)$') {
            $k = $Matches[1].Trim().ToLower(); $v = $Matches[2].Trim()
            if ($k -eq 'ai_api_key') { $apiKey = $v }
            if ($k -eq 'ai_dsh_bin') { $dshBin = $v }
        }
    }
} catch {
    Log ("读 config.ini 失败: " + $_.Exception.Message)
    exit 1
}
if (-not $apiKey) {
    Log 'config.ini [ai] ai_api_key 为空 - 请先在控制台设置里填 DeepSeek API Key'
    exit 1
}

# ---- dsh bin 定位 (SYSTEM/无 APPDATA 环境下用固定候选) ----
$candidates = @()
if ($dshBin) { $candidates += $dshBin }
$candidates += (Join-Path $env:APPDATA 'npm\node_modules\@deepseek-ai\dsh\lib\bin.js')
$candidates += (Join-Path $env:USERPROFILE 'AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\lib\bin.js')
$bin = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $bin) {
    Log '找不到 dsh bin.js (config ai_dsh_bin / APPDATA / 固定路径全落空)'
    exit 1
}
Log ("dsh bin: " + $bin)

# node.exe 定位: 优先与 dsh.cmd 同目录
$nodeExe = Join-Path $env:APPDATA 'npm\node.exe'
if (-not (Test-Path $nodeExe)) { $nodeExe = 'node' }

# ---- 单实例守卫: 3080 已有监听则退出 ----
$busy = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Log '端口 3080 已有进程监听, dsh web 疑似已在运行, 本次不重复拉起'
    exit 0
}

# ---- 启动 (前台常驻; 计划任务/服务调用时保持进程存活) ----
# SYSTEM 计划任务下 USERPROFILE 指向 systemprofile, 显式指回 Admin 的 .dsh
# (会话/凭据/工作区都在这里, 实现常驻助手的持久记忆)
$env:DSH_HOME = Join-Path $env:USERPROFILE '.dsh'
$env:DEEPSEEK_API_KEY = $apiKey
$env:DSH_NO_BROWSER_OPEN = '1'
Log '启动 dsh web: 127.0.0.1:3080 (仅回环, 由控制台 /ai 反代对外)'
# PS5.1: EAP=Stop 下原生命令 stderr 会被当作终止错误, 此处局部降级
$ErrorActionPreference = 'Continue'
# --trusted-host: 控制台域名经 /ai 反代到达时 Host 为公网域名, 必须声明信任
# (变长参数, 挪到末尾防吞掉前面的开关)
& $nodeExe $bin web --host 127.0.0.1 --port 3080 --no-open --trusted-host home.example.com 2>&1 | ForEach-Object {
    Log ("dsh: " + $_)
}
Log ("dsh web 进程退出, code=" + $LASTEXITCODE)
