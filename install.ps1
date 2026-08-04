param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir
)

$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\Codex Usage Widget'
$TargetExe = Join-Path $InstallDir 'CodexUsageWidget.exe'
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'Codex Usage.lnk'
$OldShortcut = Join-Path $Desktop 'Hermes Usage.lnk'
$OldInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\Hermes Usage Widget'

Get-Process -Name 'CodexUsageWidget','HermesUsageWidget' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $SourceDir '*') -Destination $InstallDir -Recurse -Force

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetExe
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.IconLocation = "$TargetExe,0"
$Shortcut.Description = 'Lightweight Codex quota and Hermes token monitor'
$Shortcut.Save()

if (Test-Path -LiteralPath $OldShortcut) {
    Remove-Item -LiteralPath $OldShortcut -Force
}
if (Test-Path -LiteralPath $OldInstallDir) {
    Remove-Item -LiteralPath $OldInstallDir -Recurse -Force
}
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'HermesUsageWidget' -ErrorAction SilentlyContinue

[PSCustomObject]@{
    InstalledExe = $TargetExe
    DesktopShortcut = $ShortcutPath
}
