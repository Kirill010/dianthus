# Копия базы сайта и загруженных фото НА ЭТОТ КОМПЬЮТЕР (вне сервера).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pull_backup.ps1
#
# Что делает:
#   1. просит сервер сделать свежую копию (тот же scripts/backup.sh, что работает ночью по cron);
#   2. забирает самые свежие файлы db-*.sql.gz (база) и uploads-*.tar.gz (фото) по SSH;
#   3. проверяет, что файлы целые (размер совпал, архив читается до конца);
#   4. кладёт их в папку D:\Dianthus\5-backups\site и оставляет последние 60 файлов каждого вида;
#   5. пишет журнал pull_backup.log и файл latest.txt (когда была последняя удачная копия).
#
# Адрес сервера в репозитории не хранится (репозиторий публичный): он берётся из параметра -Server, из переменной среды
# DIANTHUS_SERVER или из файла server.txt в папке копий (одна строка вида пользователь@адрес).
# Входит на сервер ключом этого компьютера (пароли скрипту не нужны и нигде не хранятся).
# Запускается сам раз в сутки задачей Планировщика (scripts\install_pull_backup.ps1); если компьютер был выключен, задача
# выполнится при ближайшем включении. Копия содержит персональные данные клиентов: храните папку только у себя.
param(
    [string]$Server = '',
    [string]$Dest = 'D:\Dianthus\5-backups\site',
    [int]$Keep = 60,
    [switch]$NoFresh
)

$ErrorActionPreference = 'Stop'
$ssh = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$scp = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
$opts = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4')

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if (-not $Server) { $Server = $env:DIANTHUS_SERVER }
$cfg = Join-Path $Dest 'server.txt'
if (-not $Server -and (Test-Path $cfg)) { $Server = (Get-Content -Path $cfg -Raw).Trim() }
$log = Join-Path $Dest 'pull_backup.log'
function Log([string]$msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Output $line
}

# выполнить команду на сервере от имени root (sudo); до трёх попыток (канал до сервера у этого компьютера бывает нестабильным)
function Invoke-Remote([string]$script, [string]$what) {
    # скрипт уходит на сервер одной ASCII-строкой base64: так нет проблем ни с кодировкой, ни с кавычками
    $body = ($script -replace "`r`n", "`n").TrimStart([char]0xFEFF)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
    for ($i = 1; $i -le 3; $i++) {
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'   # предупреждения ssh в stderr - не ошибка
        try { $out = & $ssh @opts $Server "echo $b64 | base64 -d | sudo bash" 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
        $text = (($out | ForEach-Object { "$_" }) -join "`n").Trim()
        if ($code -eq 0) { return $text }
        Log "попытка $i из 3: $what - не вышло (код $code): $text"
        Start-Sleep -Seconds (5 * $i)
    }
    throw "не удалось: $what"
}

function Test-Gzip([string]$path) {
    $fs = [IO.File]::OpenRead($path)
    try {
        $gz = New-Object IO.Compression.GZipStream($fs, [IO.Compression.CompressionMode]::Decompress)
        $buf = New-Object byte[] 65536
        while ($gz.Read($buf, 0, $buf.Length) -gt 0) { }
        return $true
    } catch { return $false } finally { $fs.Dispose() }
}

try {
    if (-not $Server) { throw "не задан сервер: параметр -Server, переменная DIANTHUS_SERVER или файл $cfg (одна строка: пользователь@адрес)" }
    Log "начало: сервер $Server, папка $Dest"
    if (-not $NoFresh) {
        $r = Invoke-Remote 'cd /root/dianthus && bash scripts/backup.sh > /tmp/dianthus_offsite_backup.log 2>&1 && echo fresh_ok' 'свежая копия на сервере'
        if ($r -notmatch 'fresh_ok') { throw "сервер не подтвердил свежую копию: $r" }
    }
    $remoteList = Invoke-Remote @'
set -e
d=/root/dianthus/backups
db=$(ls -t $d/db-*.sql.gz | head -1)
up=$(ls -t $d/uploads-*.tar.gz | head -1)
install -d -m 700 -o deploy -g deploy /home/deploy/offsite
rm -f /home/deploy/offsite/db-* /home/deploy/offsite/uploads-*
install -m 600 -o deploy -g deploy -t /home/deploy/offsite "$db" "$up"
stat -c '%n %s' /home/deploy/offsite/*
'@ 'подготовить файлы на сервере'

    $incoming = Join-Path $Dest '.incoming'
    if (Test-Path $incoming) { Remove-Item -Recurse -Force $incoming }
    New-Item -ItemType Directory -Force -Path $incoming | Out-Null
    $copied = $false
    for ($i = 1; $i -le 3 -and -not $copied; $i++) {
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & $scp @opts "${Server}:/home/deploy/offsite/*" "$incoming\" 2>&1 | Out-Null; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
        if ($code -eq 0) { $copied = $true } else { Log "скачивание, попытка $i из 3 не вышла (код $code)"; Start-Sleep -Seconds (5 * $i) }
    }
    if (-not $copied) { throw 'не удалось скачать файлы с сервера' }

    $expected = @{}
    foreach ($line in ($remoteList -split "`n")) {
        if ($line -match '^(\S+)\s+(\d+)$') { $expected[[IO.Path]::GetFileName($Matches[1])] = [int64]$Matches[2] }
    }
    if ($expected.Count -lt 2) { throw "сервер вернул неполный список файлов: $remoteList" }
    $report = @()
    foreach ($name in $expected.Keys) {
        $file = Join-Path $incoming $name
        if (-not (Test-Path $file)) { throw "файл не скачался: $name" }
        $size = (Get-Item $file).Length
        if ($size -ne $expected[$name]) { throw "размер не совпал у $name ($size вместо $($expected[$name]))" }
        if (-not (Test-Gzip $file)) { throw "архив повреждён: $name" }
        if ($name -like 'db-*' -and $size -lt 5000) { throw "дамп базы подозрительно мал: $name ($size байт)" }
        Move-Item -Force -Path $file -Destination (Join-Path $Dest $name)
        $report += "$name ($size байт)"
    }
    Remove-Item -Recurse -Force $incoming

    foreach ($pattern in 'db-*.sql.gz', 'uploads-*.tar.gz') {
        Get-ChildItem -Path $Dest -Filter $pattern | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep | Remove-Item -Force
    }
    Set-Content -Path (Join-Path $Dest 'latest.txt') -Encoding UTF8 -Value ("{0}`r`n{1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), ($report -join "`r`n"))
    Log ('ГОТОВО: ' + ($report -join '; '))
    exit 0
} catch {
    Log ('ОШИБКА: ' + $_.Exception.Message)
    exit 1
}
