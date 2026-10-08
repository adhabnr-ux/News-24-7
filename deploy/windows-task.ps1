# Registers a Windows scheduled task that starts News247 at logon and restarts it on failure.
# Run from the repo folder in an elevated PowerShell:  .\deploy\windows-task.ps1
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$exe = Join-Path $repo ".venv\Scripts\news247.exe"
$action = New-ScheduledTaskAction -Execute $exe -Argument "-c `"$repo\config.yaml`" run" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "News247" -Action $action -Trigger $trigger -Settings $settings -Force
Start-ScheduledTask -TaskName "News247"
Write-Host "News247 registered and started. Dashboard: http://127.0.0.1:8247/"
