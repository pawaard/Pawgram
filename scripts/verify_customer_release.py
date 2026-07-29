import argparse
import json
import struct
import zipfile
from pathlib import Path

ALLOWED_ROOT_ENTRIES = {".env", "Pawgram.exe", "_internal"}
ALLOWED_RUNTIME_ARCHIVES = {"_internal/base_library.zip"}
FORBIDDEN_DIRECTORY_NAMES = {
    ".codex",
    ".codex-diff-review",
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".video_review",
    "__pycache__",
    "build",
    "data",
    "dist",
    "releases",
    "scripts",
    "tests",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".diff",
    ".log",
    ".py",
    ".pyc",
    ".session",
    ".tmp",
    ".zip",
}
FORBIDDEN_ENV_KEYS = {
    "APP_SECRET_KEY",
    "DATABASE_PATH",
    "PAWGRAM_SKIP_UPDATE",
}
REQUIRED_ENV_KEYS = {
    "APP_ENV",
    "CUSTOMER_RELEASE",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "DEFAULT_PROXY_TYPE",
    "DEFAULT_PROXY_HOST",
    "DEFAULT_PROXY_PORT",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if not separator:
            raise ValueError(f"Geçersiz .env satırı: {key}")
        value = raw_value.strip()
        if value.startswith('"'):
            value = str(json.loads(value))
        values[key.strip()] = value
    return values


def windows_subsystem(executable: Path) -> int:
    with executable.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError("Pawgram.exe geçerli bir Windows PE dosyası değil.")
        handle.seek(0x3C)
        pe_offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise ValueError("Pawgram.exe PE başlığı geçersiz.")
        handle.seek(20, 1)
        optional_header = handle.read(70)
    if len(optional_header) < 70:
        raise ValueError("Pawgram.exe optional header eksik.")
    return struct.unpack_from("<H", optional_header, 68)[0]


def contains_path_marker(path: Path, marker: str) -> bool:
    normalized = marker.rstrip("\\/").strip()
    if not normalized:
        return False
    variants = {
        normalized,
        normalized.replace("\\", "/"),
        normalized.replace("/", "\\"),
    }
    needles = {
        encoded
        for variant in variants
        for encoded in (variant.encode("utf-8"), variant.encode("utf-16-le"))
        if encoded
    }
    data = path.read_bytes()
    return any(needle in data for needle in needles)


def verify_release_folder(root: Path, expected_version: str, forbidden_paths: list[str]) -> dict:
    release_root = root.resolve()
    if not release_root.is_dir():
        raise ValueError("Müşteri release klasörü bulunamadı.")
    root_entries = {item.name for item in release_root.iterdir()}
    if root_entries != ALLOWED_ROOT_ENTRIES:
        unexpected = sorted(root_entries.difference(ALLOWED_ROOT_ENTRIES))
        missing = sorted(ALLOWED_ROOT_ENTRIES.difference(root_entries))
        raise ValueError(f"Release kök içeriği geçersiz; fazla={unexpected}, eksik={missing}")

    executable = release_root / "Pawgram.exe"
    internal = release_root / "_internal"
    if not executable.is_file() or not internal.is_dir():
        raise ValueError("Pawgram.exe veya _internal klasörü eksik.")
    if windows_subsystem(executable) != 2:
        raise ValueError("Pawgram.exe Windows GUI uygulaması olarak derlenmemiş.")

    env_values = parse_env(release_root / ".env")
    missing_env = sorted(REQUIRED_ENV_KEYS.difference(env_values))
    if missing_env:
        raise ValueError(f"Müşteri .env dosyasında zorunlu alanlar eksik: {missing_env}")
    if FORBIDDEN_ENV_KEYS.intersection(env_values):
        raise ValueError("Müşteri .env dosyasında dağıtıma uygun olmayan ayar bulundu.")
    if env_values["APP_ENV"] != "production" or env_values["CUSTOMER_RELEASE"].lower() != "true":
        raise ValueError("Müşteri release üretim modunda değil.")
    if not env_values["TELEGRAM_API_ID"].isdigit() or not env_values["TELEGRAM_API_HASH"]:
        raise ValueError("Telegram başlangıç yapılandırması eksik.")
    if not env_values["DEFAULT_PROXY_HOST"] or not env_values["DEFAULT_PROXY_PORT"].isdigit():
        raise ValueError("Varsayılan proxy sağlayıcısı eksik.")

    files = [path for path in release_root.rglob("*") if path.is_file()]
    for path in files:
        relative = path.relative_to(release_root)
        lower_parts = {part.lower() for part in relative.parts}
        if FORBIDDEN_DIRECTORY_NAMES.intersection(lower_parts):
            raise ValueError(f"Release içinde geliştirme klasörü bulundu: {relative}")
        normalized_relative = relative.as_posix().lower()
        if (
            path.suffix.lower() in FORBIDDEN_SUFFIXES
            and normalized_relative not in ALLOWED_RUNTIME_ARCHIVES
        ):
            raise ValueError(f"Release içinde geçici/geliştirme dosyası bulundu: {relative}")
        for marker in forbidden_paths:
            if contains_path_marker(path, marker):
                raise ValueError(f"Release dosyasında geliştirici yolu bulundu: {relative}")

    version_file = internal / "VERSION"
    if not version_file.is_file() or version_file.read_text(encoding="utf-8").strip() != expected_version:
        raise ValueError("Release içindeki VERSION beklenen sürümle eşleşmiyor.")
    if not (internal / "RELEASE_NOTES.json").is_file():
        raise ValueError("Release notes geçmişi pakete eklenmemiş.")
    if not (internal / "static" / "index.html").is_file():
        raise ValueError("Web arayüzü pakete eklenmemiş.")
    return {"file_count": len(files), "version": expected_version}


def verify_release_zip(path: Path, expected_folder: str) -> None:
    with zipfile.ZipFile(path) as archive:
        members = [name.replace("\\", "/") for name in archive.namelist()]
    if not members or any(
        not (member == expected_folder or member.startswith(f"{expected_folder}/"))
        for member in members
    ):
        raise ValueError("Müşteri ZIP arşivi tek bir Pawgram klasörü içermeli.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--zip", dest="zip_path", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--forbid-path", action="append", default=[])
    args = parser.parse_args()
    result = verify_release_folder(args.folder, args.version, args.forbid_path)
    verify_release_zip(args.zip_path, args.folder.name)
    print(
        f"Pawgram müşteri release doğrulandı: sürüm={result['version']}, "
        f"dosya={result['file_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
