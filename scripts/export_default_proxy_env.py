import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--database", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

os.environ["DATABASE_PATH"] = str(args.database.resolve())


def load_default_proxy() -> dict | None:
    from app.config import get_settings
    from app.telegram_service import _load_default_login_proxy

    get_settings.cache_clear()
    return _load_default_login_proxy()


proxy = load_default_proxy()
if not proxy:
    raise RuntimeError("Pawgram varsayılan proxy ayarı bulunamadı.")


def env_value(value: object) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


lines = [
    "# Pawgram özel müşteri paketi - varsayılan proxy",
    f"DEFAULT_PROXY_TYPE={env_value(proxy['proxy_type'])}",
    f"DEFAULT_PROXY_HOST={env_value(proxy['host'])}",
    f"DEFAULT_PROXY_PORT={env_value(proxy['port'])}",
    f"DEFAULT_PROXY_USERNAME={env_value(proxy.get('username'))}",
    f"DEFAULT_PROXY_PASSWORD={env_value(proxy.get('password'))}",
]
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
