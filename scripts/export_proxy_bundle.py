import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

parser = argparse.ArgumentParser()
parser.add_argument("--database", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

os.environ["DATABASE_PATH"] = str(args.database.resolve())


def load_proxy() -> dict:
    from app.config import get_settings
    from app.telegram_service import _load_default_login_proxy

    get_settings.cache_clear()
    proxy = _load_default_login_proxy()
    if not proxy:
        raise RuntimeError("Pawgram varsayılan proxy ayarı bulunamadı.")
    return proxy


proxy = load_proxy()
revision = hashlib.sha256(
    json.dumps(proxy, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:24]
bundle = {
    "revision": revision,
    "proxy_type": proxy["proxy_type"],
    "host": proxy["host"],
    "port": int(proxy["port"]),
    "username": proxy.get("username"),
    "password": proxy.get("password"),
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
