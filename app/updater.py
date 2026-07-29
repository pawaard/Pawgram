from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat

# The updater invokes a fixed system executable with an argv list and signed local package paths.
import subprocess  # nosec B404
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import APP_DIR, SOURCE_DIR
from app.license_key import LICENSE_PUBLIC_KEY_PEM

UPDATE_MANIFEST_URL = (
    "https://github.com/pawaard/Pawgram/releases/latest/download/pawgram-update.json"
)
UPDATE_PUBLIC_KEY_PEM = LICENSE_PUBLIC_KEY_PEM
MAX_UPDATE_BYTES = 500 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_COMPRESSION_RATIO = 200


def current_version() -> str:
    for path in (APP_DIR / "VERSION", SOURCE_DIR / "VERSION"):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    clean = value.strip().lower().lstrip("v").split("-", 1)[0]
    try:
        return tuple(int(part) for part in clean.split("."))
    except ValueError:
        return (0,)


def is_newer_version(candidate: str, installed: str) -> bool:
    candidate_parts = _version_tuple(candidate)
    installed_parts = _version_tuple(installed)
    width = max(len(candidate_parts), len(installed_parts))
    return candidate_parts + (0,) * (width - len(candidate_parts)) > installed_parts + (0,) * (
        width - len(installed_parts)
    )


def _decode_signature(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def verify_manifest(document: dict) -> dict:
    payload = document.get("payload")
    signature = document.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise TypeError("Güncelleme manifesti eksik veya bozuk.")
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    public_key = serialization.load_pem_public_key(UPDATE_PUBLIC_KEY_PEM)
    if not isinstance(public_key, Ed25519PublicKey):
        raise TypeError("Güncelleme doğrulama anahtarı Ed25519 biçiminde değil.")
    try:
        public_key.verify(_decode_signature(signature), canonical)
    except (InvalidSignature, ValueError) as error:
        raise ValueError("Güncelleme dijital imzası geçersiz.") from error
    required = {"product", "channel", "version", "asset_url", "sha256", "archive_root"}
    if required.difference(payload):
        raise ValueError("Güncelleme manifestinde zorunlu alanlar eksik.")
    if payload["product"] != "pawgram" or payload["channel"] != "stable":
        raise ValueError("Güncelleme farklı bir ürün veya kanala ait.")
    parsed = urlparse(str(payload["asset_url"]))
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("Güncelleme paketi yalnızca resmi GitHub adresinden indirilebilir.")
    expected_prefix = "/pawaard/Pawgram/releases/download/"
    if not parsed.path.startswith(expected_prefix):
        raise ValueError("Güncelleme paketi resmi Pawgram deposuna ait değil.")
    digest = str(payload["sha256"]).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Güncelleme SHA-256 değeri geçersiz.")
    version = str(payload["version"])
    if _version_tuple(version) == (0,) or not all(
        part.isdigit() for part in version.lower().lstrip("v").split("-", 1)[0].split(".")
    ):
        raise ValueError("Güncelleme sürüm değeri geçersiz.")
    archive_root = payload["archive_root"]
    if not isinstance(archive_root, str) or not archive_root.strip():
        raise ValueError("Güncelleme arşiv kökü geçersiz.")
    root_path = PurePosixPath(archive_root.replace("\\", "/"))
    if root_path.is_absolute() or ".." in root_path.parts or len(root_path.parts) != 1:
        raise ValueError("Güncelleme arşiv kökü güvenli değil.")
    return payload


def _write_log(message: str) -> None:
    try:
        log_path = APP_DIR / "data" / "update.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass


def _download_update(url: str, destination: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    total = 0
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=httpx.Timeout(10.0, read=60.0),
        headers={"User-Agent": f"Pawgram/{current_version()}"},
    ) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPDATE_BYTES:
                    raise ValueError("Güncelleme paketi izin verilen boyutu aşıyor.")
                digest.update(chunk)
                handle.write(chunk)
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise ValueError("Güncelleme paketinin SHA-256 doğrulaması başarısız.")


def _safe_extract(archive_path: Path, destination: Path, archive_root: str) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Güncelleme arşivinde izin verilenden fazla dosya var.")
        total_uncompressed = 0
        destination_resolved = destination.resolve()
        for member in members:
            if member.flag_bits & 0x1:
                raise ValueError("Şifreli güncelleme arşivleri desteklenmiyor.")
            unix_mode = member.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise ValueError("Güncelleme arşivinde sembolik bağlantı bulunamaz.")
            total_uncompressed += member.file_size
            if total_uncompressed > MAX_EXTRACTED_BYTES:
                raise ValueError("Güncelleme arşivinin açılmış boyutu izin verilen sınırı aşıyor.")
            if (
                member.file_size > 10 * 1024 * 1024
                and member.file_size > max(1, member.compress_size) * MAX_COMPRESSION_RATIO
            ):
                raise ValueError("Güncelleme arşivinde şüpheli sıkıştırma oranı bulundu.")
            member_path = (destination / member.filename).resolve()
            if destination_resolved not in member_path.parents and member_path != destination_resolved:
                raise ValueError("Güncelleme arşivinde güvenli olmayan dosya yolu bulundu.")
        archive.extractall(destination)
    staged_root = (destination / archive_root).resolve()
    destination_resolved = destination.resolve()
    if destination_resolved not in staged_root.parents:
        raise ValueError("Güncelleme arşiv kökü güvenli değil.")
    if not (staged_root / "Pawgram.exe").is_file() or not (staged_root / "_internal").is_dir():
        raise ValueError("Güncelleme paketinde Pawgram.exe veya _internal klasörü eksik.")
    return staged_root


def mark_update_healthy() -> None:
    marker_value = os.environ.pop("PAWGRAM_UPDATE_HEALTH_FILE", "").strip()
    if not marker_value:
        return
    try:
        marker = Path(marker_value).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root not in marker.parents or not any(
            parent.name.startswith("PawgramUpdate-") for parent in marker.parents
        ):
            raise ValueError("Güncelleme sağlık işareti geçici Pawgram klasöründe değil.")
        marker.parent.mkdir(parents=True, exist_ok=True)
        pending = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
        pending.write_text(
            json.dumps(
                {"ok": True, "pid": os.getpid(), "version": current_version()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pending.replace(marker)
    except (OSError, ValueError) as error:
        _write_log(f"Güncelleme başlangıç doğrulama işareti yazılamadı: {error}")


def _updater_script() -> str:
    return r'''param(
    [Parameter(Mandatory=$true)][int]$RunningProcessId,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$StagedDir
)
$ErrorActionPreference = "Stop"
$install = [IO.Path]::GetFullPath($InstallDir)
$staged = [IO.Path]::GetFullPath($StagedDir)
$exe = Join-Path $install "Pawgram.exe"
$logDir = Join-Path $install "data"
$log = Join-Path $logDir "update.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
function Write-UpdateLog([string]$Message) {
    Add-Content -LiteralPath $log -Encoding UTF8 -Value ("[" + (Get-Date -Format "yyyy-MM-ddTHH:mm:ss") + "] " + $Message)
}
function Wait-ForProcessExit([int]$ProcessId, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -ge $deadline) {
            throw "Pawgram işlemi $TimeoutSeconds saniye içinde kapanmadı (PID: $ProcessId)."
        }
        Start-Sleep -Milliseconds 250
    }
}
function Invoke-FileOperationWithRetry(
    [scriptblock]$Operation,
    [string]$Description,
    [int]$TimeoutSeconds = 90
) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        try {
            & $Operation
            return
        } catch {
            if ((Get-Date) -ge $deadline) {
                throw ($Description + " başarısız: " + $_.Exception.Message)
            }
            Start-Sleep -Milliseconds 500
        }
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $staged "Pawgram.exe"))) { throw "Staged Pawgram.exe missing" }
if (-not (Test-Path -LiteralPath (Join-Path $staged "_internal"))) { throw "Staged _internal missing" }
Wait-ForProcessExit -ProcessId $RunningProcessId -TimeoutSeconds 90
Write-UpdateLog ("Eski Pawgram işlemi kapandı; dosya değişimi başlıyor (PID: " + $RunningProcessId + ").")
$backup = Join-Path $install (".pawgram-update-backup-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$targets = @("Pawgram.exe", "_internal")
$movedTargets = New-Object System.Collections.Generic.List[string]
$newProcess = $null
$updateRoot = Split-Path -Parent (Split-Path -Parent $staged)
$healthFile = Join-Path $updateRoot "startup-health.json"
try {
    foreach ($name in $targets) {
        $current = Join-Path $install $name
        $saved = Join-Path $backup $name
        if (Test-Path -LiteralPath $current) {
            Invoke-FileOperationWithRetry -Description ("Mevcut " + $name + " yedeklenemedi") -Operation {
                Move-Item -LiteralPath $current -Destination $saved -ErrorAction Stop
            }
            $movedTargets.Add($name)
        }
    }
    Invoke-FileOperationWithRetry -Description "Yeni Pawgram.exe kopyalanamadı" -Operation {
        Copy-Item -LiteralPath (Join-Path $staged "Pawgram.exe") -Destination $exe -Force -ErrorAction Stop
    }
    Invoke-FileOperationWithRetry -Description "Yeni _internal klasörü kopyalanamadı" -Operation {
        Copy-Item -LiteralPath (Join-Path $staged "_internal") -Destination (Join-Path $install "_internal") -Recurse -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $healthFile) { Remove-Item -LiteralPath $healthFile -Force }
    $env:PAWGRAM_UPDATE_HEALTH_FILE = $healthFile
    try {
        $newProcess = Start-Process -FilePath $exe -WorkingDirectory $install -PassThru
    } finally {
        Remove-Item Env:PAWGRAM_UPDATE_HEALTH_FILE -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $healthFile)) {
        $newProcess.Refresh()
        if ($newProcess.HasExited) {
            throw "Yeni Pawgram sürümü başlangıç doğrulamasını tamamlamadan kapandı (çıkış kodu: $($newProcess.ExitCode))."
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path -LiteralPath $healthFile)) {
        throw "Yeni Pawgram sürümü 45 saniye içinde başlangıç doğrulaması vermedi."
    }
    Start-Sleep -Seconds 2
    $newProcess.Refresh()
    if ($newProcess.HasExited) {
        throw "Yeni Pawgram sürümü başlangıçtan hemen sonra kapandı (çıkış kodu: $($newProcess.ExitCode))."
    }
    Write-UpdateLog "Güncelleme doğrulandı ve kuruldu; data klasörü korundu."
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
} catch {
    Write-UpdateLog ("Güncelleme kurulamadı, geri alınıyor: " + $_.Exception.Message)
    Remove-Item Env:PAWGRAM_UPDATE_HEALTH_FILE -ErrorAction SilentlyContinue
    if ($null -ne $newProcess) {
        $newProcess.Refresh()
        if (-not $newProcess.HasExited) {
            Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
            try { Wait-ForProcessExit -ProcessId $newProcess.Id -TimeoutSeconds 10 } catch { }
        }
    }
    foreach ($name in $movedTargets) {
        $current = Join-Path $install $name
        $saved = Join-Path $backup $name
        if (Test-Path -LiteralPath $saved) {
            if (Test-Path -LiteralPath $current) {
                Invoke-FileOperationWithRetry -Description ("Başarısız " + $name + " kaldırılamadı") -Operation {
                    Remove-Item -LiteralPath $current -Recurse -Force -ErrorAction Stop
                }
            }
            Invoke-FileOperationWithRetry -Description ("Yedek " + $name + " geri yüklenemedi") -Operation {
                Move-Item -LiteralPath $saved -Destination $current -ErrorAction Stop
            }
        }
    }
    if (Test-Path -LiteralPath $exe) { Start-Process -FilePath $exe -WorkingDirectory $install }
    exit 1
}
'''


def check_and_stage_update() -> bool:
    if not getattr(sys, "frozen", False) or os.environ.get("PAWGRAM_SKIP_UPDATE") == "1":
        return False
    try:
        response = httpx.get(
            UPDATE_MANIFEST_URL,
            follow_redirects=True,
            timeout=5.0,
            headers={"User-Agent": f"Pawgram/{current_version()}"},
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        payload = verify_manifest(response.json())
        if not is_newer_version(str(payload["version"]), current_version()):
            return False

        update_root = Path(tempfile.mkdtemp(prefix="PawgramUpdate-"))
        archive_path = update_root / "update.zip"
        extract_path = update_root / "extracted"
        extract_path.mkdir()
        _download_update(str(payload["asset_url"]), archive_path, str(payload["sha256"]))
        staged_root = _safe_extract(archive_path, extract_path, str(payload["archive_root"]))
        script_path = update_root / "install-update.ps1"
        script_path.write_text(_updater_script(), encoding="utf-8-sig")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_file():
            raise RuntimeError("Windows PowerShell sistem bileşeni bulunamadı.")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(  # nosec B603
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-RunningProcessId",
                str(os.getpid()),
                "-InstallDir",
                str(APP_DIR),
                "-StagedDir",
                str(staged_root),
            ],
            cwd=str(update_root),
            creationflags=creation_flags,
        )
        _write_log(
            f"{current_version()} sürümünden {payload['version']} sürümüne güncelleme indirildi; kurulum başlatıldı."
        )
        return True
    except Exception as error:  # noqa: BLE001 - update failure must never prevent the installed app starting
        _write_log(f"Güncelleme kontrolü atlandı: {str(error) or error.__class__.__name__}")
        return False


def clean_abandoned_update_directories() -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    for path in temp_root.glob("PawgramUpdate-*"):
        try:
            resolved = path.resolve()
            if (
                resolved.parent == temp_root
                and path.is_dir()
                and path.stat().st_mtime < __import__("time").time() - 7 * 86400
            ):
                shutil.rmtree(resolved)
        except OSError:
            pass
