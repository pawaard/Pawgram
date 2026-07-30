param(
    [string]$PythonPath = "",
    [string]$SigningKeyPath = "",
    [string]$DatabasePath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (& git -C $projectRoot status --porcelain --untracked-files=no) {
    throw "Release yalnızca commit edilmiş temiz kaynak koddan oluşturulabilir."
}
if (-not $PythonPath) {
    $runtimeRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".cache\codex-runtimes"
    $PythonPath = Get-ChildItem -LiteralPath $runtimeRoot -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -like "*dependencies\python\python.exe" -and
            $_.FullName -notlike "*.previous-*"
        } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $SigningKeyPath) {
    $SigningKeyPath = Join-Path $projectRoot "license_server\data\signing_key.pem"
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Uyumlu Python bulunamadı."
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $SigningKeyPath)) { throw "Güncelleme imzalama anahtarı bulunamadı." }
if ($DatabasePath) {
    $DatabasePath = [IO.Path]::GetFullPath($DatabasePath)
    if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
        throw "Paketlenecek müşteri proxy ayarının veritabanı bulunamadı."
    }
}

$version = (Get-Content -LiteralPath (Join-Path $projectRoot "VERSION") -Raw).Trim()
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stageRoot = Join-Path $tempRoot ("PawgramRelease-" + [Guid]::NewGuid().ToString("N"))
$sourceArchive = Join-Path $stageRoot "source.zip"
$sourceRoot = Join-Path $stageRoot "source"
$buildPackages = Join-Path $stageRoot "build-packages"
$packageRoot = Join-Path $stageRoot "package"
$packageFolder = Join-Path $packageRoot "Pawgram"
$releaseRoot = Join-Path $projectRoot "releases"
$assetName = "Pawgram-$version-win64.zip"
$assetPath = Join-Path $releaseRoot $assetName
$manifestPath = Join-Path $releaseRoot "pawgram-update.json"

New-Item -ItemType Directory -Path $stageRoot, $sourceRoot, $buildPackages, $packageFolder, $releaseRoot -Force | Out-Null
try {
    & git -C $projectRoot archive --format=zip --output=$sourceArchive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Kaynak arşivi oluşturulamadı." }
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $sourceRoot

    # GitHub güncelleme paketi müşteride mevcut ticari EXE'nin yerine geçer.
    # Bu nedenle güncelleme EXE'si de lisansı kaynak içine gömülü biçimde
    # zorunlu tutmalıdır; .env içindeki LICENSE_REQUIRED=false bunu kapatamaz.
    $editionPath = Join-Path $sourceRoot "app\edition.py"
    $editionSource = Get-Content -LiteralPath $editionPath -Raw -Encoding UTF8
    $editionSource = $editionSource -replace "COMMERCIAL_EDITION = False", "COMMERCIAL_EDITION = True"
    if ($editionSource -notmatch "COMMERCIAL_EDITION = True") {
        throw "Güncelleme paketi ticari lisans zorunluluğuyla hazırlanamadı."
    }
    Set-Content -LiteralPath $editionPath -Value $editionSource -Encoding UTF8

    & $PythonPath -m pip install --disable-pip-version-check --target $buildPackages `
        -r (Join-Path $sourceRoot "requirements.txt") `
        -r (Join-Path $sourceRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Sabitlenmiş derleme bağımlılıkları kurulamadı." }
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$buildPackages;$sourceRoot"
    try {
        if ($DatabasePath) {
            & $PythonPath (Join-Path $sourceRoot "scripts\export_proxy_bundle.py") `
                --database $DatabasePath `
                --output (Join-Path $sourceRoot "customer-proxy.json")
            if ($LASTEXITCODE -ne 0) { throw "Müşteri proxy güncelleme paketi üretilemedi." }
        }
        Push-Location -LiteralPath $sourceRoot
        try {
            & $PythonPath -m PyInstaller --noconfirm (Join-Path $sourceRoot "Pawgram.spec") `
                --distpath (Join-Path $stageRoot "dist") `
                --workpath (Join-Path $stageRoot "build")
            if ($LASTEXITCODE -ne 0) { throw "Pawgram EXE derlenemedi." }
        }
        finally {
            Pop-Location
        }
        Copy-Item -Path (Join-Path $stageRoot "dist\Pawgram\*") -Destination $packageFolder -Recurse -Force
        if (Test-Path -LiteralPath $assetPath) { Remove-Item -LiteralPath $assetPath -Force }
        Compress-Archive -Path $packageFolder -DestinationPath $assetPath -CompressionLevel Optimal
        $sha256 = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $assetUrl = "https://github.com/pawaard/Pawgram/releases/download/v$version/$assetName"
        & $PythonPath (Join-Path $sourceRoot "scripts\sign_update_manifest.py") `
            --private-key $SigningKeyPath --version $version --asset-url $assetUrl `
            --sha256 $sha256 --output $manifestPath
        if ($LASTEXITCODE -ne 0) { throw "Güncelleme manifesti imzalanamadı." }
    }
    finally {
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }
    Write-Output "Release ZIP: $assetPath"
    Write-Output "İmzalı manifest: $manifestPath"
    Write-Output "SHA-256: $sha256"
} finally {
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if ($resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
