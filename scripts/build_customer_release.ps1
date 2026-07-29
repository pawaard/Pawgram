param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (& git -C $projectRoot status --porcelain --untracked-files=no) {
    throw "Müşteri paketi yalnızca commit edilmiş temiz kaynak koddan oluşturulabilir."
}

if (-not $PythonPath) {
    $runtimeRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".cache\codex-runtimes"
    $PythonPath = Get-ChildItem -LiteralPath $runtimeRoot -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*dependencies\python\python.exe" } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw "Uyumlu Python bulunamadı. -PythonPath parametresini kullanın."
}

$databasePath = Join-Path $projectRoot "data\console.db"
if (-not (Test-Path -LiteralPath $databasePath)) {
    throw "Yerel veritabanı bulunamadı; varsayılan proxy müşteri paketine aktarılamaz."
}

$version = (Get-Content -LiteralPath (Join-Path $projectRoot "VERSION") -Raw).Trim()
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stageRoot = Join-Path $tempRoot ("PawgramCustomer-" + [Guid]::NewGuid().ToString("N"))
$sourceArchive = Join-Path $stageRoot "source.zip"
$sourceRoot = Join-Path $stageRoot "source"
$releaseRoot = Join-Path $projectRoot "releases"
$releaseFolder = Join-Path $releaseRoot "Pawgram_Musteri_${version}_${timestamp}"
$releaseZip = "$releaseFolder.zip"

New-Item -ItemType Directory -Path $stageRoot, $sourceRoot, $releaseFolder -Force | Out-Null
try {
    & git -C $projectRoot archive --format=zip --output=$sourceArchive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Kaynak arşivi oluşturulamadı." }
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $sourceRoot

    $venvRoot = Join-Path $stageRoot "build-venv"
    & $PythonPath -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Temiz derleme ortamı oluşturulamadı." }
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $sourceRoot "requirements.txt") -r (Join-Path $sourceRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Derleme bağımlılıkları kurulamadı." }
    & $venvPython -m PyInstaller --noconfirm (Join-Path $sourceRoot "Pawgram.spec") --distpath (Join-Path $stageRoot "dist") --workpath (Join-Path $stageRoot "build")
    if ($LASTEXITCODE -ne 0) { throw "Pawgram.exe derlenemedi." }

    Copy-Item -Path (Join-Path $stageRoot "dist\Pawgram\*") -Destination $releaseFolder -Recurse -Force
    Copy-Item -LiteralPath $sourceRoot -Destination (Join-Path $releaseFolder "Kaynak_Kod") -Recurse -Force

    Push-Location -LiteralPath $sourceRoot
    try {
        & $venvPython (Join-Path $sourceRoot "scripts\export_default_proxy_env.py") --database $databasePath --output (Join-Path $releaseFolder ".env")
        if ($LASTEXITCODE -ne 0) { throw "Varsayılan proxy müşteri paketine aktarılamadı." }
    }
    finally {
        Pop-Location
    }

    Compress-Archive -Path (Join-Path $releaseFolder "*") -DestinationPath $releaseZip -CompressionLevel Optimal
    Write-Output "Müşteri klasörü: $releaseFolder"
    Write-Output "Müşteri ZIP: $releaseZip"
}
finally {
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if ($resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
