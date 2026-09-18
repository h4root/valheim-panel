# Runs the Valheim dedicated server on Windows.
# Stop with Ctrl+C ONCE in this same window, wait for "World save (5/5) done".
# Closing the window with the X does not save.

$ErrorActionPreference = "Stop"

$ServerName    = "My Valheim Server"
$WorldName     = "Dedicated"
$Port          = 2456
$Public        = 0             # 0 hidden (code/IP only), 1 listed in the server browser
$Crossplay     = $true
$InstanceId    = ""            # only needed with multiple servers on this MAC address

# A preset overrides every modifier below, if set.
$Preset        = ""            # normal casual easy hard hardcore immersive hammer

# World modifiers — empty means "don't set it" (engine default).
$Combat        = ""            # veryeasy easy hard veryhard
$DeathPenalty  = ""            # casual veryeasy easy hard hardcore
$Resources     = ""            # muchless less more muchmore most
$Raids         = ""            # none muchless less more muchmore
$Portals       = ""            # casual hard veryhard

$SetKeys       = @()           # nobuildcost playerevents passivemobs nomap

$SaveInterval  = 1800
$Backups       = 4
$BackupShort   = 7200
$BackupLong    = 43200

# Steam install path — this is the default location, change it if yours differs.
$ServerDir = "C:\Program Files (x86)\Steam\steamapps\common\Valheim dedicated server"
$Root      = $PSScriptRoot
$SaveDir   = Join-Path $Root "save"
$PassFile  = Join-Path $Root ".password"
$LogDir    = Join-Path $Root "logs"
$BackupDir = Join-Path $Root "backups"

if (-not (Test-Path $PassFile)) {
    Write-Host "No password file: $PassFile"
    Write-Host "Create it (PowerShell):  'yourpassword' | Set-Content -NoNewline '$PassFile'"
    exit 1
}
$Password = Get-Content $PassFile -Raw
$Password = $Password.Trim()
if ($Password.Length -lt 5) {
    Write-Host "Password shorter than 5 characters — the server will reject it."
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir, $BackupDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

$WorldDir = Join-Path $SaveDir "worlds_local\$WorldName"
if (Test-Path $WorldDir) {
    $BackupFile = Join-Path $BackupDir "${WorldName}_$Stamp.zip"
    Compress-Archive -Path $WorldDir -DestinationPath $BackupFile
    Write-Host "World backup: $BackupFile"
    Get-ChildItem $BackupDir -Filter "${WorldName}_*.zip" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 5 |
        Remove-Item -Force
} else {
    Write-Host "NOTE: world $WorldName does not exist yet — the server will generate it."
}

$ExePath = Join-Path $ServerDir "valheim_server.exe"
if (-not (Test-Path $ExePath)) {
    Write-Host "Not found: $ExePath — check your Steam install path (currently set to: $ServerDir)."
    exit 1
}

$ArgList = @(
    "-name", $ServerName,
    "-port", $Port,
    "-world", $WorldName,
    "-password", $Password,
    "-savedir", $SaveDir,
    "-public", $Public,
    "-saveinterval", $SaveInterval,
    "-backups", $Backups,
    "-backupshort", $BackupShort,
    "-backuplong", $BackupLong
)
if ($Crossplay)    { $ArgList += "-crossplay" }
if ($InstanceId)   { $ArgList += @("-instanceid", $InstanceId) }
if ($Preset)       { $ArgList += @("-preset", $Preset) }
if ($Combat)       { $ArgList += @("-modifier", "combat", $Combat) }
if ($DeathPenalty) { $ArgList += @("-modifier", "deathpenalty", $DeathPenalty) }
if ($Resources)    { $ArgList += @("-modifier", "resources", $Resources) }
if ($Raids)        { $ArgList += @("-modifier", "raids", $Raids) }
if ($Portals)      { $ArgList += @("-modifier", "portals", $Portals) }
foreach ($key in $SetKeys) { $ArgList += @("-setkey", $key) }

$Log = Join-Path $LogDir "server_$Stamp.log"
Write-Host "World: $WorldName   Saving every $($SaveInterval / 60) min   Log: $Log"
Write-Host "Stop with Ctrl+C ONCE in this window, wait for 'World save (5/5) done'."
Write-Host ""

# --- keep Windows from sleeping while the server runs ---
Add-Type -Namespace Win32 -Name Power -MemberDefinition @"
[DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
$ES_CONTINUOUS       = [uint32]2147483648   # 0x80000000
$ES_SYSTEM_REQUIRED  = [uint32]1            # 0x00000001
[Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null

# Lower the server process priority so it doesn't compete with everything
# else on the machine. Done as a background job because the & $ExePath call
# below blocks until the server itself exits.
Start-Job -ScriptBlock {
    for ($i = 0; $i -lt 60; $i++) {
        $proc = Get-Process -Name "valheim_server" -ErrorAction SilentlyContinue
        if ($proc) { $proc.PriorityClass = "BelowNormal"; break }
        Start-Sleep -Milliseconds 500
    }
} | Out-Null

# No pipeline/Tee-Object: on Windows a PowerShell pipeline can intercept
# Ctrl+C before it reaches the child process. To follow the log live, open
# a second PowerShell window and run:
#   Get-Content -Path "<path printed above>" -Wait -Tail 30
Push-Location $ServerDir
try {
    & $ExePath @ArgList *> $Log
} finally {
    Pop-Location
    [Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
}
