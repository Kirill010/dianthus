# Автообмен 1С с сайтом «Диантус»: создаёт задачу Планировщика заданий Windows.
#
# Запускать ОДИН РАЗ на компьютере 1С, в окне PowerShell, от имени того пользователя Windows, под которым должен идти обмен
# (этот пользователь должен быть «в системе» — открытый или свёрнутый сеанс, выход из системы остановит задачу).
#
#   .\install_auto_exchange.ps1                                  # вход в 1С по пользователю Windows (если так настроено)
#   .\install_auto_exchange.ps1 -OneCUser "Обмен"               # вход по имени пользователя 1С (спросит пароль)
#   .\install_auto_exchange.ps1 -EveryMinutes 15 -From 07:30 -To 22:00
#
# ЧТО ДЕЛАЕТ: каждые N минут в рабочее время запускает 1С без окон: обработка ОбменССайтом.epf с параметром «авто» отправляет
# инвойсы на сайт (если в меню обработки, пункт 12, включена автоматическая отправка инвойсов), пишет отчёт в
# Документы\Dianthus1C\last_report.txt и историю в auto_log.txt.
#
# ВАЖНО
#  * Для входа без окна 1С нужен пользователь 1С БЕЗ интерактивного ввода: либо вход по пользователю Windows, либо имя и пароль
#    (пароль попадёт в параметры задачи — лучше завести отдельного пользователя 1С «Обмен» с минимальными правами на чтение).
#  * Скрипт НЕ включает автоматическую отправку: это делает пункт 12 обработки (иначе «авто» ничего не отправит).
#  * Удалить задачу: Unregister-ScheduledTask -TaskName "Dianthus - обмен 1С с сайтом" -Confirm:$false
param(
    [string]$Epf = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'ОбменССайтом.epf'),
    [string]$Base = 'Цветы',
    [string]$OneCUser = '',
    [int]$EveryMinutes = 30,
    [string]$From = '08:00',
    [string]$To = '21:00',
    [string]$V8 = ''
)

$ErrorActionPreference = 'Stop'
$taskName = 'Dianthus - обмен 1С с сайтом'

if (-not (Test-Path $Epf)) { throw "Не найден файл обработки: $Epf" }
if ($V8 -eq '') {
    $V8 = (Get-ChildItem 'C:\Program Files*\1cv8\*\bin\1cv8.exe' -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1).FullName
}
if (-not $V8 -or -not (Test-Path $V8)) { throw 'Не нашёл 1cv8.exe: укажите путь параметром -V8' }

$auth = ''
if ($OneCUser -ne '') {
    $secure = Read-Host "Пароль пользователя 1С «$OneCUser» (Enter, если пароля нет)" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $auth = " /N`"$OneCUser`""
    if ($plain -ne '') { $auth += " /P`"$plain`"" }
}

$out = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Dianthus1C\task_out.txt'
$arguments = "ENTERPRISE /IBName`"$Base`"$auth /Execute`"$Epf`" /C`"авто`" /DisableStartupMessages /DisableStartupDialogs /Out`"$out`""
$action = New-ScheduledTaskAction -Execute $V8 -Argument $arguments

# каждый день с $From, повтор каждые $EveryMinutes минут до $To
$start = [datetime]::ParseExact($From, 'HH:mm', $null)
$stop = [datetime]::ParseExact($To, 'HH:mm', $null)
$hours = [math]::Max(1, [int][math]::Ceiling(($stop - $start).TotalHours))
$trigger = New-ScheduledTaskTrigger -Daily -At $start
$repeat = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) -RepetitionDuration (New-TimeSpan -Hours $hours)
$trigger.Repetition = $repeat.Repetition

$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Задача «$taskName» создана для ${me}: каждые $EveryMinutes мин с $From до $To."
Write-Host "1) В обработке (пункт 12) включите автоматическую отправку ИНВОЙСОВ."
Write-Host "2) Проверьте вручную: Start-ScheduledTask -TaskName `"$taskName`""
Write-Host "3) Через минуту посмотрите Документы\Dianthus1C\auto_log.txt и плашку «Обмен работает» в админке сайта."
