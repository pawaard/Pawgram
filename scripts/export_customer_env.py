import argparse
import hashlib
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--database", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

os.environ["DATABASE_PATH"] = str(args.database.resolve())


def load_customer_configuration() -> tuple[int, str, dict]:
    from app.config import get_settings
    from app.database import get_app_setting
    from app.security import decrypt
    from app.telegram_service import _load_default_login_proxy

    get_settings.cache_clear()
    api_id = get_app_setting("telegram_api_id")
    encrypted_hash = get_app_setting("telegram_api_hash_encrypted")
    if not api_id or not encrypted_hash:
        raise RuntimeError("Müşteri paketi için Telegram bağlantı bilgileri yapılandırılmamış.")
    proxy = _load_default_login_proxy()
    if not proxy:
        raise RuntimeError("Müşteri paketi için varsayılan proxy sağlayıcısı yapılandırılmamış.")
    return int(api_id), decrypt(encrypted_hash), proxy


def env_value(value: object) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


telegram_api_id, telegram_api_hash, default_proxy = load_customer_configuration()
proxy_revision = hashlib.sha256(
    json.dumps(default_proxy, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:24]
lines = [
    "APP_ENV=production",
    "APP_PORT=8000",
    "CUSTOMER_RELEASE=true",
    "LICENSE_REQUIRED=true",
    "LICENSE_SERVER_URL=https://license.rewmarket.com",
    f"TELEGRAM_API_ID={telegram_api_id}",
    f"TELEGRAM_API_HASH={env_value(telegram_api_hash)}",
    f"DEFAULT_PROXY_TYPE={env_value(default_proxy['proxy_type'])}",
    f"DEFAULT_PROXY_HOST={env_value(default_proxy['host'])}",
    f"DEFAULT_PROXY_PORT={env_value(default_proxy['port'])}",
    f"DEFAULT_PROXY_USERNAME={env_value(default_proxy.get('username'))}",
    f"DEFAULT_PROXY_PASSWORD={env_value(default_proxy.get('password'))}",
    f"DEFAULT_PROXY_REVISION={proxy_revision}",
]
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
