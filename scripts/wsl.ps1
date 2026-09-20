# Run a lab Makefile target inside the Containerlab WSL distro from Windows.
# Usage: scripts\wsl.ps1 up | down | golden | check | serve | stop-serve | test-lab
# Set NETTWIN_WSL_DISTRO to override the distro name (default: Containerlab).
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Target)
$ErrorActionPreference = "Stop"
$distro = if ($env:NETTWIN_WSL_DISTRO) { $env:NETTWIN_WSL_DISTRO } else { "Containerlab" }
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$drive = $root.Substring(0, 1).ToLower()
$wslPath = "/mnt/$drive" + $root.Substring(2).Replace("\", "/")
$targets = if ($Target) { $Target -join " " } else { "help" }
& wsl.exe -d $distro -- bash -lc "cd '$wslPath' && make -C lab $targets"
exit $LASTEXITCODE
