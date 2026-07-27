import ctypes
import multiprocessing
import os
import socket
import sys
import threading
import webbrowser

# PyInstaller'ın penceresiz Windows derlemesinde standart çıktı akışları None
# olabilir. Uvicorn ve bazı bağımlılıklar bu akışları kontrol ettiği için EXE
# daha port açmadan kapanmasın.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import uvicorn

from app.config import get_settings
from app.main import app
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


def open_panel(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}/")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        clean_abandoned_update_directories()
        if check_and_stage_update():
            raise SystemExit(0)
        settings = get_settings()
        port = available_port(settings.app_port)
        threading.Timer(1.4, open_panel, args=(port,)).start()
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            reload=False,
            access_log=False,
            log_level="warning",
        )
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Pawgram başlatılamadı:\n\n{error}",
            "Pawgram",
            0x10,
        )
