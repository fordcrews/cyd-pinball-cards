# Build plugin\bin\Release\CydPinballCards.dll (install.ps1 builds and installs in one go).
#   powershell -ExecutionPolicy Bypass -File frontends\launchbox\plugin\build.ps1 [-LaunchBox D:\LaunchBox]
param([string]$LaunchBox = "")
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Resolve-Path (Join-Path $Here "..\..\..")
$sdk = Join-Path $Repo ".tools\dotnet\dotnet.exe"
if (-not (Test-Path $sdk)) { $sdk = "dotnet" }   # any .NET 10 SDK on PATH
if (-not $LaunchBox) { $LaunchBox = if ($env:LAUNCHBOX_HOME) { $env:LAUNCHBOX_HOME } else { Join-Path $env:USERPROFILE "LaunchBox" } }
& $sdk build (Join-Path $Here "CydPinballCards.csproj") -c Release -v minimal "-p:LaunchBoxDir=$LaunchBox"
