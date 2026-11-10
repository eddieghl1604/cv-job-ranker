# Stops the background job agent (it will start again at your next login).
#     powershell -ExecutionPolicy Bypass -File .\stop_agent.ps1
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("pythonw.exe", "python.exe")) -and ($_.CommandLine -like "*jobagent.run*")
}
if (-not $procs) { Write-Host "The agent isn't running."; exit 0 }
$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Stopped $(@($procs).Count) agent process(es)." -ForegroundColor Green
