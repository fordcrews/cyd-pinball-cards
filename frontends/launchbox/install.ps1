# Build and install the CydPinballCards LaunchBox plugin (reversible).
# Does not edit LaunchBox XML, does not delete files, does not close LaunchBox.
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Resolve-Path (Join-Path $Here "..\..")
$PluginSrc = Join-Path $Here "plugin"
$Lb = "C:\Users\fcrews\LaunchBox"
if (-not (Test-Path (Join-Path $Lb "Core\Unbroken.LaunchBox.Plugins.dll"))) {
  throw "LaunchBox Plugins DLL not found under $Lb\Core"
}
$Dotnet = $null
foreach ($c in @(
  (Join-Path $Repo ".tools\dotnet\dotnet.exe"),
  "dotnet"
)) {
  if ($c -eq "dotnet") {
    $cmd = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($cmd) { $Dotnet = $cmd.Source; break }
  } elseif (Test-Path $c) { $Dotnet = $c; break }
}
if (-not $Dotnet) { throw "No .NET SDK / portable dotnet found. See README.md" }

Write-Host "Building with $Dotnet"
& $Dotnet build (Join-Path $PluginSrc "CydPinballCards.csproj") -c Release
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$dll = Join-Path $PluginSrc "bin\Release\CydPinballCards.dll"
if (-not (Test-Path $dll)) {
  $dll = Get-ChildItem (Join-Path $PluginSrc "bin\Release") -Recurse -Filter CydPinballCards.dll |
    Select-Object -First 1 -ExpandProperty FullName
}
if (-not $dll) { throw "CydPinballCards.dll not found after build" }

$dest = Join-Path $Lb "Plugins\CydPinballCards"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$target = Join-Path $dest "CydPinballCards.dll"
if (Test-Path $target) {
  # Back up outside Plugins (LaunchBox loads every DLL under Plugins).
  $bk = Join-Path $env:USERPROFILE ("cyd-backups\CydPinballCards-plugin-" + (Get-Date -Format yyyyMMdd-HHmmss))
  New-Item -ItemType Directory -Force -Path $bk | Out-Null
  Copy-Item $target $bk
  try { Copy-Item -Force $dll $target -ErrorAction Stop }
  catch {
    # LaunchBox is running and holds the DLL: rename it aside; the new one loads on restart.
    Rename-Item $target ("CydPinballCards.dll.old-" + (Get-Date -Format yyyyMMddHHmmss))
    Copy-Item $dll $target
  }
} else {
  Copy-Item -Force $dll $target
}

$py = $null
foreach ($name in @("pythonw.exe", "python.exe")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) { $py = "pythonw" }

$cfgPath = Join-Path $dest "cyd_launchbox.cfg"
if (Test-Path $cfgPath) {
  Write-Host "Keeping existing $cfgPath"
} else {
  $cfgText = "# CydPinballCards LaunchBox plugin config`r`nCYD_HOME=$Repo`r`nPYTHON=$py`r`n" +
    "# Start host\cyd_daemon.py when LaunchBox starts and nothing listens on 127.0.0.1:47291 (0 = off)`r`n" +
    "AUTOSTART_DAEMON=1`r`nDAEMON_ARGS=--profile arcade --log %TEMP%\cyd-daemon.log`r`n"
  [System.IO.File]::WriteAllText($cfgPath, $cfgText, (New-Object System.Text.UTF8Encoding $false))
}

Write-Host "Installed to $dest"
Write-Host "Restart LaunchBox / Big Box to load the plugin (this script did not close it)."
