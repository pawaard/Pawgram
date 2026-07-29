param(
    [string]$PythonPath = "",
    [string]$DatabasePath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".git"))) {
    throw "Müşteri release yalnızca Git çalışma kopyasından oluşturulabilir."
}
if (& git -C $projectRoot status --porcelain) {
    throw "Müşteri release yalnızca commit edilmiş temiz kaynak koddan oluşturulabilir."
}

if (-not $PythonPath) {
    $candidates = @(
        $env:PAWGRAM_PYTHON,
        (Join-Path $projectRoot ".venv314\Scripts\python.exe"),
        (Join-Path $projectRoot ".venv\Scripts\python.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $PythonPath = $candidates | Select-Object -First 1
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw "Uyumlu Python bulunamadı. -PythonPath parametresini kullanın."
}
if (-not $DatabasePath) {
    $DatabasePath = Join-Path $projectRoot "data\console.db"
}
$DatabasePath = [IO.Path]::GetFullPath($DatabasePath)
if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
    throw "Telegram ve varsayılan proxy başlangıç ayarlarının bulunduğu veritabanı bulunamadı."
}

$version = (Get-Content -LiteralPath (Join-Path $projectRoot "VERSION") -Raw).Trim()
$versionResource = Get-Content -LiteralPath (Join-Path $projectRoot "assets\pawgram-version-info.txt") -Raw
if ($versionResource -notmatch [regex]::Escape("ProductVersion', u'$version'")) {
    throw "Windows sürüm kaynağı VERSION dosyasıyla eşleşmiyor."
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stageRoot = Join-Path $tempRoot ("PawgramCustomer-" + [Guid]::NewGuid().ToString("N"))
$sourceArchive = Join-Path $stageRoot "source.zip"
$sourceRoot = Join-Path $stageRoot "source"
$buildPackages = Join-Path $stageRoot "build-packages"
$deliveryRoot = Join-Path $projectRoot "releases\Pawgram_Musteri_$($version)_$timestamp"
$packageFolder = Join-Path $deliveryRoot "Pawgram"
$releaseZip = Join-Path $projectRoot "releases\Pawgram-Customer-$version-win64.zip"

New-Item -ItemType Directory -Path $stageRoot, $sourceRoot, $buildPackages, $packageFolder -Force | Out-Null
try {
    & git -C $projectRoot archive --format=zip --output=$sourceArchive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Commit edilmiş kaynak arşivi oluşturulamadı." }
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $sourceRoot

    & $PythonPath -m pip install --disable-pip-version-check --target $buildPackages `
        -r (Join-Path $sourceRoot "requirements.txt") `
        -r (Join-Path $sourceRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Sabitlenmiş release bağımlılıkları kurulamadı." }

    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$buildPackages;$sourceRoot"
    Push-Location -LiteralPath $sourceRoot
    try {
        & $PythonPath (Join-Path $sourceRoot "scripts\export_customer_env.py") `
            --database $DatabasePath `
            --output (Join-Path $packageFolder ".env")
        if ($LASTEXITCODE -ne 0) { throw "Müşteri başlangıç yapılandırması üretilemedi." }

        & $PythonPath -m PyInstaller --noconfirm (Join-Path $sourceRoot "Pawgram.Customer.spec") `
            --distpath (Join-Path $stageRoot "dist") `
            --workpath (Join-Path $stageRoot "build")
        if ($LASTEXITCODE -ne 0) { throw "Pawgram müşteri EXE dosyası derlenemedi." }
    }
    finally {
        Pop-Location
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }

    Copy-Item -Path (Join-Path $stageRoot "dist\Pawgram\*") -Destination $packageFolder -Recurse -Force

    $versionInfo = (Get-Item -LiteralPath (Join-Path $packageFolder "Pawgram.exe")).VersionInfo
    if ($versionInfo.ProductVersion -ne $version -or $versionInfo.CompanyName -ne "Paward") {
        throw "Pawgram.exe sürüm veya yayıncı bilgisi doğrulanamadı."
    }

    if (Test-Path -LiteralPath $releaseZip) {
        Remove-Item -LiteralPath $releaseZip -Force
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $deliveryRoot,
        $releaseZip,
        [IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    & $PythonPath (Join-Path $sourceRoot "scripts\verify_customer_release.py") `
        --folder $packageFolder `
        --zip $releaseZip `
        --version $version `
        --forbid-path $projectRoot `
        --forbid-path $stageRoot
    if ($LASTEXITCODE -ne 0) { throw "Müşteri release içerik denetiminden geçemedi." }

    $sha256 = (Get-FileHash -LiteralPath $releaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output "Müşteri klasörü: $packageFolder"
    Write-Output "Müşteri ZIP: $releaseZip"
    Write-Output "SHA-256: $sha256"
}
finally {
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if (
        $resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedStage)
    ) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
