# Ежедневная копия базы сайта на этот компьютер: создаёт задачу Планировщика Windows «Dianthus - копия базы сайта вне сервера».
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pull_backup.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pull_backup.ps1 -At 12:00
#
# Задача запускает scripts\pull_backup.ps1 каждый день в указанное время от имени ТЕКУЩЕГО пользователя Windows
# (нужен его SSH-ключ; администратором быть не нужно). Если компьютер в это время был выключен, копия сделается при
# ближайшем включении (StartWhenAvailable). Удалить задачу:
#   Unregister-ScheduledTask -TaskName "Dianthus - копия базы сайта вне сервера" -Confirm:$false
param(
    [string]$At = '13:00',
    [string]$Script = (Join-Path $PSScriptRoot 'pull_backup.ps1')
)

$ErrorActionPreference = 'Stop'
$taskName = 'Dianthus - копия базы сайта вне сервера'
if (-not (Test-Path $Script)) { throw "Не найден скрипт: $Script" }

$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, 'HH:mm', $null))
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Скачивает свежую копию базы сайта dianthus-opt.ru и фото на этот компьютер (папка D:\Dianthus\5-backups\site).' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $taskName
Write-Output ("Задача создана: {0}; запуск каждый день в {1}; пользователь {2}; следующий запуск: {3}" -f $taskName, $At, $me, (Get-ScheduledTaskInfo -TaskName $taskName).NextRunTime)
