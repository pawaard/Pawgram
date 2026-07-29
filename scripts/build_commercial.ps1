param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$LicenseServerUrl,

    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (& git -C $projectRoot status --porcelain --untracked-files=no) {
    throw "Ticari paket yalnızca commit edilmiş temiz kaynak koddan oluşturulabilir."
}

if (-not $PythonPath) {
    $configuredPython = $env:PAWGRAM_PYTHON
    if ($configuredPython -and (Test-Path -LiteralPath $configuredPython)) {
        $PythonPath = $configuredPython
    } else {
        $runtimeRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".cache\codex-runtimes"
        $PythonPath = Get-ChildItem -LiteralPath $runtimeRoot -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*dependencies\python\python.exe" } |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw "Uyumlu Python bulunamadı. -PythonPath parametresini kullanın."
}

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stageRoot = Join-Path $tempRoot ("PawgramCommercial-" + [Guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $stageRoot "source.zip"
$sourceRoot = Join-Path $stageRoot "source"

New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
try {
    & git -C $projectRoot archive --format=zip --output=$archivePath HEAD
    if ($LASTEXITCODE -ne 0) { throw "Kaynak arşivi oluşturulamadı." }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $sourceRoot

    $editionPath = Join-Path $sourceRoot "app\edition.py"
    $editionSource = Get-Content -LiteralPath $editionPath -Raw -Encoding UTF8
    $editionSource = $editionSource -replace "COMMERCIAL_EDITION = False", "COMMERCIAL_EDITION = True"
    Set-Content -LiteralPath $editionPath -Value $editionSource -Encoding UTF8

    $venvRoot = Join-Path $stageRoot "build-venv"
    & $PythonPath -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Temiz derleme ortamı oluşturulamadı." }
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $sourceRoot "requirements.txt") -r (Join-Path $sourceRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Sabitlenmiş derleme bağımlılıkları kurulamadı." }
    & $venvPython -m PyInstaller --noconfirm (Join-Path $sourceRoot "Pawgram.spec") --distpath (Join-Path $stageRoot "dist") --workpath (Join-Path $stageRoot "build")
    if ($LASTEXITCODE -ne 0) { throw "Ticari EXE derlenemedi." }

    $version = (Get-Content -LiteralPath (Join-Path $sourceRoot "VERSION") -Raw).Trim()
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $releaseRoot = Join-Path $projectRoot "releases"
    $releaseFolder = Join-Path $releaseRoot "Pawgram_Commercial_${version}_${timestamp}"
    New-Item -ItemType Directory -Path $releaseFolder -Force | Out-Null
    Copy-Item -Path (Join-Path $stageRoot "dist\Pawgram\*") -Destination $releaseFolder -Recurse -Force
    Set-Content -LiteralPath (Join-Path $releaseFolder ".env") -Encoding UTF8 -Value @(
        "LICENSE_REQUIRED=true",
        "LICENSE_SERVER_URL=$($LicenseServerUrl.TrimEnd('/'))"
    )
    Set-Content -LiteralPath (Join-Path $releaseFolder "LISANS_BILGISI.txt") -Encoding UTF8 -Value @(
        "Pawgram ticari sürüm $version",
        "Bu paket lisans sunucusu doğrulaması olmadan Telegram işlemi çalıştırmaz.",
        "Lisans sunucusu: $($LicenseServerUrl.TrimEnd('/'))"
    )
    $releaseZip = "$releaseFolder.zip"
    Compress-Archive -Path (Join-Path $releaseFolder "*") -DestinationPath $releaseZip -CompressionLevel Optimal
    Write-Output "Ticari paket: $releaseFolder"
    Write-Output "Teslim ZIP: $releaseZip"
}
finally {
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if ($resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
