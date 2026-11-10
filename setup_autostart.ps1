# Makes the job agent start by itself, hidden, every time you log in to Windows,
# and starts it right now. Run once from the project folder:
#     powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw  = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
    Write-Host "Can't find $pyw - create the virtual environment first." -ForegroundColor Red
    exit 1
}

# 1. Check everything imports before installing (pythonw shows no errors, so test with python.exe)
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -c "import telethon, anthropic, gspread, yaml, bs4, pydantic, requests, dotenv"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Some packages are missing. Run: .venv\Scripts\pip install telethon python-dotenv anthropic pydantic pyyaml requests beautifulsoup4 truststore gspread" -ForegroundColor Red
    exit 1
}

# 2. Shortcut in your Startup folder (runs at every login, no admin rights needed)
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "Job Agent.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath       = $pyw
$lnk.Arguments        = "-m jobagent.run --background"
$lnk.WorkingDirectory = $root
$lnk.WindowStyle      = 7
$lnk.Description      = "Telegram job-screening agent"
$lnk.Save()
Write-Host "Auto-start installed: $lnkPath" -ForegroundColor Green

# 3. Start it now (if it's already running, the new copy just exits)
Start-Process -FilePath $pyw -ArgumentList "-m jobagent.run --background" -WorkingDirectory $root -WindowStyle Hidden
Write-Host "Agent started in the background. Your bot will say 'Job agent started' in a few seconds."
Write-Host "Log file: $root\data\agent.log"
