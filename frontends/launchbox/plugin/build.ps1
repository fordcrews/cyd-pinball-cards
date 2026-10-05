$ErrorActionPreference = "Stop"
$sdk = "C:\Users\fcrews\projects\cyd-pinball-cards\.tools\dotnet\dotnet.exe"
$proj = "C:\Users\fcrews\projects\cyd-pinball-cards\frontends\launchbox\plugin\CydPinballCards.csproj"
& $sdk build $proj -c Release -v minimal
