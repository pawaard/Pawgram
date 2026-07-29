import ctypes
import http.client
import json
import multiprocessing
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import BinaryIO

# PyInstaller'ın penceresiz Windows derlemesinde standart çıktı akışları None
# olabilir. Uvicorn ve bazı bağımlılıklar bu akışları kontrol ettiği için EXE
# daha port açmadan kapanmasın.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - process-lifetime stream
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - process-lifetime stream

import uvicorn

from app.config import APP_DIR, get_settings
from app.main import app
from app.runtime_control import set_shutdown_callback
from app.updater import check_and_stage_update, clean_abandoned_update_directories


def available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Pawgram için kullanılabilir yerel port bulunamadı.")


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    self._handle.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


def find_running_panel(preferred: int) -> int | None:
    for port in range(preferred, preferred + 20):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            if response.status != 200:
                continue
            payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok") is True and payload.get("app") == "Pawgram":
                return port
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
        finally:
            connection.close()
    return None


def open_panel(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}/")


def show_message(message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            0,
            message,
            "Pawgram",
            0x10 if error else 0x40,
        )
    else:
        print(message, file=sys.stderr if error else sys.stdout)


def main() -> int:
    multiprocessing.freeze_support()
    settings = get_settings()
    instance_lock = SingleInstanceLock(APP_DIR / "data" / "pawgram.lock")
    if not instance_lock.acquire():
        running_port = find_running_panel(settings.app_port)
        if running_port is not None:
            open_panel(running_port)
            show_message("Pawgram zaten çalışıyor. Açık yönetim paneli tarayıcıda gösterildi.")
        else:
            show_message(
                "Pawgram zaten çalışıyor ancak panel portu bulunamadı. Birkaç saniye sonra yeniden deneyin.",
                error=True,
            )
        return 0
    try:
        clean_abandoned_update_directories()
        if check_and_stage_update():
            return 0
        port = available_port(settings.app_port)
        browser_timer = threading.Timer(1.4, open_panel, args=(port,))
        browser_timer.daemon = True
        browser_timer.start()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                reload=False,
                access_log=False,
                log_level="warning",
            )
        )
        set_shutdown_callback(lambda: setattr(server, "should_exit", True))
        try:
            server.run()
        finally:
            set_shutdown_callback(None)
        return 0
    except Exception as error:  # noqa: BLE001 - final GUI boundary must report startup failures
        show_message(f"Pawgram başlatılamadı:\n\n{error}", error=True)
        return 1
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
