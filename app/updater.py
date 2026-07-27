from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
import zipfile

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
        raise ValueError("Güncelleme manifesti eksik veya bozuk.")
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    public_key: Ed25519PublicKey = serialization.load_pem_public_key(UPDATE_PUBLIC_KEY_PEM)
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
    return payload


def _write_log(message: str) -> None:
    try:
        log_path = APP_DIR / "data" / "update.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
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
        destination_resolved = destination.resolve()
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if destination_resolved not in member_path.parents and member_path != destination_resolved:
                raise ValueError("Güncelleme arşivinde güvenli olmayan dosya yolu bulundu.")
        archive.extractall(destination)
    staged_root = destination / archive_root
    if not (staged_root / "Pawgram.exe").is_file() or not (staged_root / "_internal").is_dir():
        raise ValueError("Güncelleme paketinde Pawgram.exe veya _internal klasörü eksik.")
    return staged_root


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
if (-not (Test-Path -LiteralPath (Join-Path $staged "Pawgram.exe"))) { throw "Staged Pawgram.exe missing" }
if (-not (Test-Path -LiteralPath (Join-Path $staged "_internal"))) { throw "Staged _internal missing" }
try { Wait-Process -Id $RunningProcessId -Timeout 90 -ErrorAction SilentlyContinue } catch { }
$backup = Join-Path $install (".pawgram-update-backup-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$targets = @("Pawgram.exe", "_internal")
try {
    foreach ($name in $targets) {
        $current = Join-Path $install $name
        if (Test-Path -LiteralPath $current) { Move-Item -LiteralPath $current -Destination (Join-Path $backup $name) }
    }
    Copy-Item -LiteralPath (Join-Path $staged "Pawgram.exe") -Destination $exe -Force
    Copy-Item -LiteralPath (Join-Path $staged "_internal") -Destination (Join-Path $install "_internal") -Recurse -Force
    Write-UpdateLog "Güncelleme kuruldu; data klasörü korundu."
    Start-Process -FilePath $exe -WorkingDirectory $install
    Start-Sleep -Seconds 3
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
} catch {
    Write-UpdateLog ("Güncelleme kurulamadı, geri alınıyor: " + $_.Exception.Message)
    foreach ($name in $targets) {
        $current = Join-Path $install $name
        $saved = Join-Path $backup $name
        if (Test-Path -LiteralPath $current) { Remove-Item -LiteralPath $current -Recurse -Force }
        if (Test-Path -LiteralPath $saved) { Move-Item -LiteralPath $saved -Destination $current }
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
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell.exe",
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
    except Exception as error:
        _write_log(f"Güncelleme kontrolü atlandı: {str(error) or error.__class__.__name__}")
        return False


def clean_abandoned_update_directories() -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    for path in temp_root.glob("PawgramUpdate-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < __import__("time").time() - 7 * 86400:
                shutil.rmtree(path)
        except OSError:
            pass
