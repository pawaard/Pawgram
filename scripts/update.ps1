param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$logDirectory = Join-Path $ProjectRoot "data"
$logPath = Join-Path $logDirectory "update.log"

function Write-UpdateLog {
    param([string]$Message)
    if (-not (Test-Path -LiteralPath $logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "[$timestamp] $Message" -Encoding UTF8
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".git"))) {
        exit 0
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        Write-UpdateLog "Git bulunamadığı için güncelleme kontrolü atlandı."
        exit 0
    }

    Push-Location -LiteralPath $ProjectRoot
    try {
        $remote = (& git remote get-url origin 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or $remote -notmatch "github\.com[/:]pawaard/Pawgram(?:\.git)?$") {
            Write-UpdateLog "Beklenen Pawgram GitHub deposu bulunamadı; güncelleme atlandı."
            exit 0
        }

        $dirty = & git status --porcelain --untracked-files=no
        if ($LASTEXITCODE -ne 0) {
            throw "Git çalışma ağacı denetlenemedi."
        }
        if ($dirty) {
            Write-UpdateLog "Yerel kod değişiklikleri bulundu; dosyaları korumak için güncelleme atlandı."
            exit 0
        }

        $requirements = Join-Path $ProjectRoot "requirements.txt"
        $oldRequirementsHash = if (Test-Path -LiteralPath $requirements) {
            (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
        } else { "" }

        & git fetch origin main --quiet
        if ($LASTEXITCODE -ne 0) {
            throw "GitHub güncelleme bilgisi alınamadı. GitHub oturumunu kontrol edin."
        }

        $localCommit = (& git rev-parse HEAD).Trim()
        $remoteCommit = (& git rev-parse origin/main).Trim()
        if ($localCommit -eq $remoteCommit) {
            exit 0
        }

        & git merge-base --is-ancestor HEAD origin/main
        if ($LASTEXITCODE -ne 0) {
            Write-UpdateLog "Yerel ve uzak sürüm ayrışmış; otomatik birleştirme yapılmadı."
            exit 0
        }

        & git pull --ff-only origin main --quiet
        if ($LASTEXITCODE -ne 0) {
            throw "Güncelleme indirilemedi."
        }

        $newRequirementsHash = if (Test-Path -LiteralPath $requirements) {
            (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
        } else { "" }

        if ($oldRequirementsHash -ne $newRequirementsHash -and (Test-Path -LiteralPath $PythonPath)) {
            $packages = Join-Path $ProjectRoot ".packages"
            & $PythonPath -m pip install --disable-pip-version-check --target $packages --upgrade -r $requirements
            if ($LASTEXITCODE -ne 0) {
                Write-UpdateLog "Kod güncellendi fakat Python gereksinimleri tam kurulamadı."
            }
        }

        Write-UpdateLog "Pawgram $localCommit sürümünden $remoteCommit sürümüne güncellendi."
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-UpdateLog ("Güncelleme hatası: " + $_.Exception.Message)
    exit 0
}
