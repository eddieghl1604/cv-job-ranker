# Stops the agent starting at login (and stops it now).
#     powershell -ExecutionPolicy Bypass -File .\remove_autostart.ps1
$lnkPath = Join-Path ([Environment]::GetFolderPath("Startup")) "Job Agent.lnk"
if (Test-Path $lnkPath) { Remove-Item $lnkPath; Write-Host "Auto-start removed." -ForegroundColor Green }
else { Write-Host "Auto-start wasn't installed." }
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "stop_agent.ps1")
