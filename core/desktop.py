#!/usr/bin/env python3
"""My Bookshelf Windows desktop launcher."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_TITLE = "My Bookshelf"
DEFAULT_PORT = 8501
HERE = Path(__file__).resolve().parent
APP_SCRIPT = HERE / "pipeline_app.py"


def _find_app_icon() -> str:
    for base in (HERE, HERE.parent, HERE.parent / "platform" / "windows"):
        p = base / "MyBookshelf.ico"
        if p.exists():
            return str(p)
    return str(HERE.parent / "MyBookshelf.ico")


APP_ICON = _find_app_icon()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_free_port(start: int = DEFAULT_PORT) -> int:
    if _port_in_use(start):
        return start
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def _server_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _start_streamlit(port: int) -> subprocess.Popen | None:
    if _port_in_use(port) and _server_ready(port):
        return None
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_SCRIPT),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--global.developmentMode",
        "false",
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(HERE.parent),
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )


def main() -> int:
    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "pywebview is not installed.\n"
            "Run setup.bat again or install pywebview in the venv.\n"
        )
        return 1

    port = _find_free_port(DEFAULT_PORT)
    proc = _start_streamlit(port)

    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + 60
    while time.time() < deadline:
        if _server_ready(port):
            break
        time.sleep(0.4)
    else:
        sys.stderr.write("Streamlit did not start in time.\n")
        if proc:
            proc.terminate()
        return 1

    webview.create_window(
        APP_TITLE,
        url,
        width=1280,
        height=1040,
        min_size=(980, 820),
        text_select=True,
    )
    icon = APP_ICON if os.path.exists(APP_ICON) else None

    def _apply_win32_icon() -> None:
        if sys.platform != "win32" or not icon:
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
            if not hwnd:
                return
            hicon = ctypes.windll.user32.LoadImageW(None, icon, 1, 0, 0, 0x10 | 0x40)
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon)
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon)
        except Exception:
            pass

    try:
        webview.start(icon=icon, func=_apply_win32_icon)
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
