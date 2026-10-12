# Start cyd_daemon (with night sleep + idle info screens) at every Windows logon, and restart it if
# it exits. Adds one shortcut to your Startup folder; -Uninstall removes it. No admin rights needed.
#   powershell -ExecutionPolicy Bypass -File host\windows\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File host\windows\install-autostart.ps1 -DaemonArgs "--profile arcade --exclude-port COM5 --log %TEMP%\cyd-daemon.log"
#   powershell -ExecutionPolicy Bypass -File host\windows\install-autostart.ps1 -Uninstall
param(
  [string]$DaemonArgs = "--profile arcade --log %TEMP%\cyd-daemon.log",
  [switch]$Uninstall,
  [switch]$StartNow
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$Lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "cyd-daemon.lnk"
if ($Uninstall) {
  if (Test-Path $Lnk) { Remove-Item $Lnk; Write-Host "Removed $Lnk" } else { Write-Host "Not installed" }
  return
}
$py = $null
foreach ($name in @("pythonw.exe", "python.exe")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) { throw "Python 3 not found on PATH" }
$sup = Join-Path $Repo "host\cyd_supervise.py"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($Lnk)
$s.TargetPath = $py
$s.Arguments = "`"$sup`" -- $DaemonArgs"
$s.WorkingDirectory = $Repo
$s.WindowStyle = 7
$s.Description = "cyd-pinball-cards daemon (supervised)"
$s.Save()
Write-Host "Installed $Lnk"
Write-Host "  $py `"$sup`" -- $DaemonArgs"
if ($StartNow) { Start-Process -FilePath $py -ArgumentList "`"$sup`" -- $DaemonArgs" -WorkingDirectory $Repo -WindowStyle Hidden }
