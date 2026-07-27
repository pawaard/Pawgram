import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from license_server.config import get_license_server_settings


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def generate_signing_keys(private_path: Path, public_path: Path) -> None:
    if private_path.exists():
        raise FileExistsError(f"Private key already exists: {private_path}")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def _private_key() -> Ed25519PrivateKey:
    path = get_license_server_settings().signing_key_path
    if not path.exists():
        raise RuntimeError("License signing key is missing. Run generate_keys.py first.")
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def sign_payload(payload: dict) -> str:
    encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{encoded}.{_encode(_private_key().sign(encoded.encode('ascii')))}"


def verify_token(token: str) -> dict:
    encoded, signature = token.split(".", 1)
    public_key: Ed25519PublicKey = serialization.load_pem_public_key(
        get_license_server_settings().public_key_path.read_bytes()
    )
    try:
        public_key.verify(_decode(signature), encoded.encode("ascii"))
    except InvalidSignature as error:
        raise ValueError("Invalid license signature") from error
    return json.loads(_decode(encoded))
