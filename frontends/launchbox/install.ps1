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
Copy-Item -Force $dll (Join-Path $dest "CydPinballCards.dll")

$py = $null
foreach ($name in @("pythonw.exe", "python.exe")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) { $py = "pythonw" }

$cfgText = "# CydPinballCards LaunchBox plugin config`r`nCYD_HOME=$Repo`r`nPYTHON=$py`r`n"
[System.IO.File]::WriteAllText((Join-Path $dest "cyd_launchbox.cfg"), $cfgText)

Write-Host "Installed to $dest"
Write-Host "Restart LaunchBox / Big Box to load the plugin (this script did not close it)."
