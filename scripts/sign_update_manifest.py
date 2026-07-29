import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


parser = argparse.ArgumentParser()
parser.add_argument("--private-key", required=True, type=Path)
parser.add_argument("--version", required=True)
parser.add_argument("--asset-url", required=True)
parser.add_argument("--sha256", required=True)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

payload = {
    "product": "pawgram",
    "channel": "stable",
    "version": args.version,
    "asset_url": args.asset_url,
    "sha256": args.sha256.lower(),
    "archive_root": "Pawgram",
    "published_at": datetime.now(UTC).isoformat(),
}
canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
private_key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
if not isinstance(private_key, Ed25519PrivateKey):
    raise TypeError("Güncelleme imzalama anahtarı Ed25519 biçiminde olmalıdır.")
document = {"payload": payload, "signature": encode(private_key.sign(canonical))}
args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
