#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ImagePath = (Join-Path $PSScriptRoot "photo\1.1.jpg"),
    [string]$FolderId,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ocrSecret = $null
$ocrCredential = $null
$ocrHeaders = $null
$ocrBody = $null
$ocrBytes = $null
$ocrExitCode = 0

try {
    $ocrFile = Get-Item -LiteralPath $ImagePath -ErrorAction Stop
    if ($ocrFile.PSIsContainer) {
        throw "Expected an image file."
    }
    $ocrMime = switch ($ocrFile.Extension.ToLowerInvariant()) {
        ".jpg" { "JPEG" }
        ".jpeg" { "JPEG" }
        ".png" { "PNG" }
        default { throw "Expected JPEG or PNG." }
    }
    if ($ocrFile.Length -eq 0 -or $ocrFile.Length -gt 8MB) {
        throw "Expected a nonempty image up to 8 MB."
    }

    $ocrBytes = [System.IO.File]::ReadAllBytes($ocrFile.FullName)
    $ocrIsJpeg = $ocrBytes.Length -ge 3 -and $ocrBytes[0] -eq 255 -and $ocrBytes[1] -eq 216 -and $ocrBytes[2] -eq 255
    $ocrIsPng = $ocrBytes.Length -ge 8 -and ([BitConverter]::ToString($ocrBytes, 0, 8) -eq "89-50-4E-47-0D-0A-1A-0A")
    if (($ocrMime -eq "JPEG" -and -not $ocrIsJpeg) -or ($ocrMime -eq "PNG" -and -not $ocrIsPng)) {
        throw "Image signature does not match its extension."
    }

    if ($CheckOnly) {
        Write-Host ("Локальная проверка пройдена: " + $ocrFile.Name + ", " + $ocrMime + ", " + $ocrBytes.Length + " байт.")
        Write-Host "Ключ не запрашивался. Запросов к Яндексу и списаний не было."
        return
    }

    Write-Host "Будет отправлена одна фотография в Яндекс: один платный OCR-запрос, без YandexGPT."
    if ([string]::IsNullOrWhiteSpace($FolderId)) {
        $FolderId = Read-Host "ID каталога Yandex Cloud (не ID облака)"
    }
    $FolderId = $FolderId.Trim()
    if ($FolderId -notmatch '^[a-zA-Z0-9_-]+$') {
        throw "Invalid folder ID."
    }
    $ocrSecret = Read-Host "Секретное значение API-ключа" -AsSecureString
    if ($ocrSecret.Length -eq 0) {
        throw "Empty API key."
    }
    $ocrCredential = [System.Net.NetworkCredential]::new("", $ocrSecret)

    $ocrBody = @{
        mimeType = $ocrMime
        languageCodes = @("ru", "en")
        model = "handwritten"
        content = [Convert]::ToBase64String($ocrBytes)
    } | ConvertTo-Json -Depth 5

    $ocrHeaders = @{
        Authorization = "Api-Key " + $ocrCredential.Password
        "x-folder-id" = $FolderId
        "x-data-logging-enabled" = "false"
    }

    $ocrRequest = @{
        Method = "Post"
        Uri = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"
        Headers = $ocrHeaders
        ContentType = "application/json; charset=utf-8"
        Body = [System.Text.Encoding]::UTF8.GetBytes($ocrBody)
        TimeoutSec = 60
        ErrorAction = "Stop"
    }
    # Decode raw response bytes explicitly: Windows PowerShell may guess a wrong charset.
    $ocrWebResponse = Invoke-WebRequest @ocrRequest -UseBasicParsing
    $ocrStream = $ocrWebResponse.RawContentStream
    if ($ocrStream.CanSeek) { $ocrStream.Position = 0 }
    $ocrReader = [System.IO.StreamReader]::new($ocrStream, [System.Text.Encoding]::UTF8, $true)
    try {
        $ocrResponse = $ocrReader.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop
    }
    finally {
        $ocrReader.Dispose()
    }

    $ocrAnnotation = $ocrResponse.result.textAnnotation
    if ($null -eq $ocrAnnotation) {
        $ocrAnnotation = $ocrResponse.textAnnotation
    }
    if ($null -eq $ocrAnnotation) {
        Write-Host "Сервис ответил, но ожидаемый результат OCR отсутствует."
        $ocrExitCode = 1
    } elseif ([string]::IsNullOrWhiteSpace($ocrAnnotation.fullText)) {
        Write-Host "Ответ получен, но текст не найден. Проверь качество фото."
    } else {
        Write-Host "Распознанный текст:"
        $ocrAnnotation.fullText
    }
}
catch {
    if ($null -ne $_.Exception.Response) {
        $ocrStatus = [int]$_.Exception.Response.StatusCode
        Write-Host ("Ошибка HTTP: " + $ocrStatus)
        switch ($ocrStatus) {
            401 { Write-Host "Проверь секретное значение и срок действия ключа." }
            403 { Write-Host "Проверь ai.vision.user на каталоге, область действия ключа и платёжный аккаунт." }
            400 { Write-Host "Проверь формат, размер изображения (до 20 мегапикселей) и параметры запроса." }
            429 { Write-Host "Достигнут лимит запросов. Повтори позже." }
            default { Write-Host "Проверь доступность сервиса и настройки Яндекс Cloud." }
        }
    } else {
        Write-Host "Проверка не завершена. Проверь путь к JPEG/PNG, размер до 8 МБ, ID каталога и подключение."
    }
    $ocrExitCode = 1
}
finally {
    if ($null -ne $ocrSecret) { $ocrSecret.Dispose() }
    Remove-Variable ocrSecret, ocrCredential, ocrHeaders, ocrBody, ocrBytes, ocrRequest -ErrorAction SilentlyContinue
}

if ($ocrExitCode -ne 0) { exit $ocrExitCode }
