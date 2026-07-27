from license_server.config import get_license_server_settings
from license_server.signing import generate_signing_keys
from app.config import SOURCE_DIR


if __name__ == "__main__":
    settings = get_license_server_settings()
    generate_signing_keys(settings.signing_key_path, settings.public_key_path)
    public_pem = settings.public_key_path.read_text(encoding="ascii")
    module_path = SOURCE_DIR / "app" / "license_key.py"
    module_path.write_text(
        "# Generated public verification key. Never place the private key here.\n"
        f"LICENSE_PUBLIC_KEY_PEM = b\"\"\"{public_pem}\"\"\"\n",
        encoding="utf-8",
    )
    print(f"Public key created: {settings.public_key_path}")
    print(f"Client verification module created: {module_path}")
    print("Keep the private signing key secret and back it up securely.")
