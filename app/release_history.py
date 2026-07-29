import json
from functools import lru_cache

from app.config import RESOURCE_DIR
from app.database import get_app_setting, set_app_setting

RELEASE_HISTORY_PATH = RESOURCE_DIR / "RELEASE_NOTES.json"
LAST_STARTED_VERSION_SETTING = "last_started_version"
PENDING_RELEASE_NOTES_SETTING = "pending_release_notes_version"
SEEN_RELEASE_NOTES_SETTING = "release_notes_seen_version"


@lru_cache
def release_history() -> list[dict]:
    try:
        document = json.loads(RELEASE_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(document, list):
        return []
    history: list[dict] = []
    for item in document:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version") or "").strip()
        release_date = str(item.get("release_date") or "").strip()
        title = str(item.get("title") or "").strip()
        changes = item.get("changes")
        if not version or not release_date or not title or not isinstance(changes, list):
            continue
        normalized_changes = [str(change).strip() for change in changes if str(change).strip()]
        history.append(
            {
                "version": version,
                "release_date": release_date,
                "title": title,
                "changes": normalized_changes,
            }
        )
    return history


def initialize_release_tracking(current_version: str) -> None:
    previous_version = get_app_setting(LAST_STARTED_VERSION_SETTING)
    if previous_version is None:
        set_app_setting(LAST_STARTED_VERSION_SETTING, current_version)
        set_app_setting(SEEN_RELEASE_NOTES_SETTING, current_version)
        return
    if previous_version != current_version:
        set_app_setting(PENDING_RELEASE_NOTES_SETTING, current_version)
        set_app_setting(LAST_STARTED_VERSION_SETTING, current_version)


def release_notes_overview(current_version: str) -> dict:
    pending_version = get_app_setting(PENDING_RELEASE_NOTES_SETTING)
    seen_version = get_app_setting(SEEN_RELEASE_NOTES_SETTING)
    history = release_history()
    current = next((item for item in history if item["version"] == current_version), None)
    return {
        "current_version": current_version,
        "pending_version": pending_version if pending_version and pending_version != seen_version else None,
        "current": current,
        "history": history,
    }


def acknowledge_release_notes(version: str, current_version: str) -> dict:
    if version != current_version:
        raise ValueError("Yalnızca mevcut sürümün notları kapatılabilir.")
    set_app_setting(SEEN_RELEASE_NOTES_SETTING, version)
    set_app_setting(PENDING_RELEASE_NOTES_SETTING, "")
    return release_notes_overview(current_version)
