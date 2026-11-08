# sensor_daemon.ps1 - read hardware sensors and write sensors.json every 2s
# 存储部分: 每块物理盘独立输出 (disks 数组), 并每 60s 刷新一次
# Get-PhysicalDisk 健康状态与 SMART 可靠性计数器 (需 SYSTEM/管理员, 本守护以 SYSTEM 运行)
$ErrorActionPreference = 'Stop'

$libPath = 'D:\Monitor\LibreHardwareMonitor\LibreHardwareMonitorLib.dll'
$outFile = 'D:\Monitor\sensors.json'
$tmpFile = 'D:\Monitor\sensors.json.tmp'
$intervalMs = 2000

Add-Type -Path $libPath

$comp = New-Object LibreHardwareMonitor.Hardware.Computer
$comp.IsCpuEnabled = $true
$comp.IsMotherboardEnabled = $true
$comp.IsStorageEnabled = $true
$comp.IsMemoryEnabled = $true
$comp.IsNetworkEnabled = $false
$comp.IsGpuEnabled = $true
$comp.IsControllerEnabled = $true
$comp.Open()

function Get-AcpiZones {
    $zones = @()
    try {
        $t = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop
        foreach ($z in $t) {
            $c = [math]::Round(($z.CurrentTemperature / 10 - 273.15), 1)
            if ($c -gt -50 -and $c -lt 150) {
                $zones += @{ name = $z.InstanceName; value = $c }
            }
        }
    } catch { }
    return $zones
}

function Update-Hardware([object]$hw) {
    try { $hw.Update() } catch { }
    foreach ($sub in $hw.SubHardware) { Update-Hardware $sub }
}

function Collect-Sensors([object]$hw, [hashtable]$acc) {
    foreach ($s in $hw.Sensors) {
        if ($null -eq $s.Value) { continue }
        $key = $hw.HardwareType.ToString() + '/' + $s.SensorType.ToString() + '/' + $s.Name
        if (-not $acc.ContainsKey($key)) {
            $acc[$key] = @{ name = $key; type = $s.SensorType.ToString(); value = [math]::Round($s.Value, 2) }
        } else {
            $acc[$key].value = [math]::Round($s.Value, 2)
        }
    }
    foreach ($sub in $hw.SubHardware) { Collect-Sensors $sub $acc }
}

# ---------- 物理盘信息 (健康/盘符/SMART 计数器): 每 60s 刷新一次并缓存 ----------
$script:physCache = @()
$script:physStamp = [datetime]::MinValue      # DateTime-DateTime 才无重载歧义
function Update-PhysInfo {
    if (((Get-Date) - $script:physStamp).TotalSeconds -lt 60) { return }
    $script:physStamp = Get-Date
    try {
        $script:physCache = @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
            $pd = $_
            $rc = $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
            $letters = ''
            try {
                $letters = (Get-Partition -DiskNumber $pd.DeviceId -ErrorAction Stop |
                    Where-Object DriveLetter | ForEach-Object { "$($_.DriveLetter):" }) -join ','
            } catch { }
            [PSCustomObject]@{
                model   = [string]$pd.Model
                health  = [string]$pd.HealthStatus
                media   = [string]$pd.MediaType
                bus     = [string]$pd.BusType
                size_gb = [math]::Round($pd.Size / 1GB)
                disk_id = [string]$pd.DeviceId
                letters = $letters
                hours   = $rc.PowerOnHours
                temp    = $rc.Temperature
                wear    = $rc.Wear
            }
        })
    } catch {
        $script:physCache = @()
    }
}

function Match-Phys([object]$hwNode) {
    $m = [string]$hwNode.Name
    $mn = ($m -replace '\s+', ' ').ToLower().Trim()
    foreach ($p in $script:physCache) {
        $pn = ($p.model -replace '\s+', ' ').ToLower().Trim()
        if ($pn -and ($pn -eq $mn -or $pn.Contains($mn) -or $mn.Contains($pn))) { return $p }
    }
    return $null
}

# 每块存储硬件 → 一个磁盘条目 (LHM 传感器 + 物理盘信息合并)
function Build-DiskEntry([object]$hw) {
    $temp = $null; $hours = $null; $life = $null
    $readGb = $null; $writtenGb = $null; $act = $null
    foreach ($s in $hw.Sensors) {
        if ($null -eq $s.Value) { continue }
        $v = [math]::Round($s.Value, 2)
        switch -Regex ($s.SensorType.ToString() + '/' + $s.Name) {
            '^Temperature/' { $temp = $v }
            '^Factor/Power On Hours' { $hours = $v }
            '^Level/Life' { $life = $v }
            '^Data/Data Read' { $readGb = $v }
            '^Data/Data Written' { $writtenGb = $v }
            '^Load/' { $act = $v }
        }
    }
    $entry = [ordered]@{
        idx      = ($hw.Identifier -replace '[^0-9]', '')
        model    = [string]$hw.Name
        temp     = $temp
        hours    = $hours
        life     = $life
        read_gb  = $readGb
        write_gb = $writtenGb
        activity = $act
        health   = $null
        media    = $null
        bus      = $null
        letters  = ''
        size_gb  = $null
    }
    $phys = Match-Phys $hw
    if ($phys) {
        $entry.health = $phys.health
        $entry.media = $phys.media
        $entry.bus = $phys.bus
        $entry.letters = $phys.letters
        $entry.size_gb = $phys.size_gb
        if ($null -eq $entry.hours -and $phys.hours) { $entry.hours = $phys.hours }
        if ($null -eq $entry.temp -and $phys.temp) { $entry.temp = $phys.temp }
    }
    return [PSCustomObject]$entry     # PSCustomObject 防止 $disks += 时被 IDictionary 枚举拆散
}

$loop = 0
while ($true) {
try {
    $loop++
    Update-PhysInfo                    # 物理盘健康信息每 60s 刷新一次
    $acc = @{}
    $disks = @()
    foreach ($hw in $comp.Hardware) {
        Update-Hardware $hw
        # 存储设备走专用的 disks 通道 (每盘独立), 不再混入全局传感器 (多盘同名会互相覆盖)
        if ($hw.HardwareType.ToString() -eq 'Storage') {
            $disks += Build-DiskEntry $hw
            continue
        }
        Collect-Sensors $hw $acc
    }

    $sensors = @($acc.Values | Sort-Object name)
    $cpuTemp = $null; $cpuPower = $null
    foreach ($s in $sensors) {
        if ($s.type -eq 'Temperature' -and $s.name -like 'Cpu/Temperature/CPU Package') { $cpuTemp = $s.value }
        if ($s.type -eq 'Power' -and $s.name -like 'Cpu/Power/CPU Package') { $cpuPower = [math]::Round($s.value, 3) }
    }

    $result = [ordered]@{
        ts          = [int][double]::Parse((Get-Date -UFormat %s))
        cpu_temp    = $cpuTemp
        cpu_power   = $cpuPower
        acpi_zones  = Get-AcpiZones
        sensors     = $sensors
        disks       = $disks
    }

    try {
        $json = $result | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText($tmpFile, $json, [System.Text.Encoding]::UTF8)
        Move-Item -Force $tmpFile $outFile
    } catch {
        Write-Output ("write error: " + $_.Exception.Message)
    }

    Start-Sleep -Milliseconds $intervalMs
} catch {
    "[$(Get-Date -Format 'HH:mm:ss')] daemon fatal: $($_.Exception.Message) @ $($_.InvocationInfo.ScriptLineNumber)" |
        Out-File 'D:\Monitor\app\daemon_error.log' -Append -Encoding utf8
    Start-Sleep -Seconds 5
}                                  # catch
}                                  # while
