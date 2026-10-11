param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [switch]$Launch,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\Codex Usage Widget'
$TargetExe = Join-Path $InstallDir 'CodexUsageWidget.exe'
$SourceExe = Join-Path $SourceDir 'CodexUsageWidget.exe'
if (!(Test-Path -LiteralPath $SourceExe)) { throw 'Source package has no executable.' }
if ($SourceDir.TrimEnd('\') -eq $InstallDir.TrimEnd('\')) { throw 'Source must be separate from the installed app.' }
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Stage = "$InstallDir.staging-$Stamp"
$Backup = "$InstallDir.rollback-$Stamp"
$SourceHash = (Get-FileHash -LiteralPath $SourceExe -Algorithm SHA256).Hash

function Get-WidgetProcesses {
    @(Get-Process -Name 'CodexUsageWidget' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $TargetExe })
}
function Assert-Package($Directory) {
    $ManifestPath = Join-Path $Directory 'build-info.json'
    if (!(Test-Path -LiteralPath $ManifestPath)) { throw 'Package is missing its file-integrity manifest.' }
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    foreach ($Entry in $Manifest.files) {
        $Relative = [string]$Entry.path
        if ([IO.Path]::IsPathRooted($Relative) -or ($Relative -split '[\\/]') -contains '..') { throw 'Unsafe manifest path.' }
        $File = Join-Path $Directory $Relative
        if ((Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash -ne $Entry.sha256) {
            throw "Package integrity check failed: $Relative"
        }
    }
}
Assert-Package $SourceDir
if ($ValidateOnly) { Write-Output 'Package integrity OK'; return }
# Stop only this installed widget via its normal shutdown path. Never force-kill
# Hermes or overwrite a live executable; abort if graceful shutdown fails.
if ((Get-WidgetProcesses).Count) {
    $Client = New-Object Net.Sockets.TcpClient
    try {
        if (!$Client.ConnectAsync('127.0.0.1', 47631).Wait(1500)) { throw 'Widget IPC unavailable; quit it from the tray, then retry.' }
        $Bytes = [Text.Encoding]::ASCII.GetBytes("QUIT`n")
        $Client.GetStream().Write($Bytes, 0, $Bytes.Length)
    } finally { $Client.Dispose() }
    $Deadline = (Get-Date).AddSeconds(15)
    while ((Get-WidgetProcesses).Count -and (Get-Date) -lt $Deadline) { Start-Sleep -Milliseconds 100 }
    if ((Get-WidgetProcesses).Count) { throw 'Widget did not exit. Nothing was replaced.' }
}
$SavedSettings = Join-Path $env:LOCALAPPDATA 'CodexUsageWidget\settings.json'
$SettingsBackup = "$SavedSettings.before-$Stamp"
if (Test-Path -LiteralPath $SavedSettings) { Copy-Item -LiteralPath $SavedSettings -Destination $SettingsBackup }
$Swapped = $false
try {
    New-Item -ItemType Directory -Path $Stage | Out-Null
    Copy-Item -Path (Join-Path $SourceDir '*') -Destination $Stage -Recurse
    Assert-Package $Stage
    if (Test-Path -LiteralPath $InstallDir) { Move-Item -LiteralPath $InstallDir -Destination $Backup }
    Move-Item -LiteralPath $Stage -Destination $InstallDir
    $Swapped = $true
    if ((Get-FileHash -LiteralPath $TargetExe -Algorithm SHA256).Hash -ne $SourceHash) { throw 'Installed executable hash mismatch.' }
    if ($Launch) {
        $Process = Start-Process -FilePath $TargetExe -WorkingDirectory $InstallDir -PassThru
        Start-Sleep -Seconds 2
        if ($Process.HasExited) { throw 'The updated widget exited during startup.' }
    }
    $Shell = New-Object -ComObject WScript.Shell
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Codex Usage.lnk'
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $TargetExe
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.IconLocation = "$TargetExe,0"
    $Shortcut.Description = 'API Usage - provider quotas and Hermes token history'
    $Shortcut.Save()
    # Startup opt-in and the existing executable path are intentionally unchanged.
    [PSCustomObject]@{ InstalledExe=$TargetExe; SHA256=$SourceHash; Rollback=$Backup; SettingsBackup=$SettingsBackup; DesktopShortcut=$ShortcutPath }
} catch {
    if ($Swapped -and !(Get-WidgetProcesses).Count) {
        if (Test-Path -LiteralPath $InstallDir) { Move-Item -LiteralPath $InstallDir -Destination "$InstallDir.failed-$Stamp" }
        if (Test-Path -LiteralPath $Backup) { Move-Item -LiteralPath $Backup -Destination $InstallDir }
        if (Test-Path -LiteralPath $SettingsBackup) { Copy-Item -LiteralPath $SettingsBackup -Destination $SavedSettings -Force }
    } elseif (!(Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $Backup)) {
        Move-Item -LiteralPath $Backup -Destination $InstallDir
    }
    throw
}
