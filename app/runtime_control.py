from __future__ import annotations

import threading
from collections.abc import Callable

_callback_lock = threading.Lock()
_shutdown_callback: Callable[[], None] | None = None


def set_shutdown_callback(callback: Callable[[], None] | None) -> None:
    global _shutdown_callback
    with _callback_lock:
        _shutdown_callback = callback


def request_shutdown() -> bool:
    with _callback_lock:
        callback = _shutdown_callback
    if callback is None:
        return False
    callback()
    return True


def shutdown_available() -> bool:
    with _callback_lock:
        return _shutdown_callback is not None


def schedule_shutdown(delay_seconds: float = 0.75) -> bool:
    if not shutdown_available():
        return False
    timer = threading.Timer(delay_seconds, request_shutdown)
    timer.daemon = True
    timer.start()
    return True
