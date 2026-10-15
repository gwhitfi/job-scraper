param(
    [string]$Distribution = "Ubuntu",
    [string]$LinuxUser = "justin"
)
$ErrorActionPreference = "Stop"
$taskName = "JobScraper WSL"
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
# A Windows-owned WSL process keeps the distro alive after terminals close.
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wsl.exe" `
    -Argument "-d $Distribution -u $LinuxUser --exec /bin/sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -Hidden
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Start WSL at login and keep the local Job Scraper timer available." | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
