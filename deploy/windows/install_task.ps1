# Install the monitor as a Windows Scheduled Task that starts at logon and
# restarts itself if it ever stops.
#
#   Right-click PowerShell -> Run as Administrator, then:
#     cd C:\Users\Lenovo\OneDrive\Desktop\fortnite_shop
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
#
# Remove it again with:
#     Unregister-ScheduledTask -TaskName "FortniteShopMonitor" -Confirm:$false

param(
    [string]$TaskName = "FortniteShopMonitor",
    [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\..\..").Path
)

$ErrorActionPreference = "Stop"

Write-Host "Project directory: $ProjectDir"

# Prefer a project venv if one exists, else fall back to python on PATH.
$VenvPythonw = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
if (Test-Path $VenvPythonw) {
    $PythonExe = $VenvPythonw
} else {
    $Found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $Found) {
        $Found = Get-Command python.exe -ErrorAction SilentlyContinue
    }
    if ($null -eq $Found) {
        throw "Could not find python. Install Python or create a .venv in the project."
    }
    $PythonExe = $Found.Source
}
Write-Host "Python: $PythonExe"

$RunScript = Join-Path $ProjectDir "run.py"
if (-not (Test-Path $RunScript)) {
    throw "run.py not found at $RunScript"
}

# pythonw runs without a console window, so it sits quietly in the background.
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "`"$RunScript`"" -WorkingDirectory $ProjectDir

# Start at logon, and also right after a reboot.
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn
$TriggerStartup = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 = run forever

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing task '$TaskName'."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $TriggerLogon, $TriggerStartup `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Monitors the Fortnite Item Shop and alerts on watchlist items." | Out-Null

Write-Host ""
Write-Host "Installed scheduled task '$TaskName'."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it with:      Get-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:               $ProjectDir\logs\monitor.log"
