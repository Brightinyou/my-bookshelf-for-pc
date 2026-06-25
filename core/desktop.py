#!/usr/bin/env python3
"""My Bookshelf 데스크톱 런처 (네이티브 창).

Streamlit 서버를 백그라운드(헤드리스)로 띄우고, 주소창 없는 네이티브 창에
표시한다. 창을 닫으면 서버도 함께 종료된다.

- macOS: WebKit (pyobjc) 사용 — OS 내장
- Windows: Edge WebView2 (Chromium) 사용 — OS 내장(Win11 기본)

브라우저 탭이 아니라 독립 앱처럼 보이게 하는 것이 목적. PyInstaller·공증과
무관하며, 기존 스크립트 설치 방식 위에서 그대로 동작한다.
"""
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
APP_ICON = str(HERE.parent / "MyBookshelf.ico")


def _port_in_use(port: int) -> bool:
    """해당 포트에서 이미 서버가 떠 있는지 확인."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_free_port(start: int = DEFAULT_PORT) -> int:
    """start부터 비어 있는 포트를 찾는다. (이미 떠 있으면 그 포트 재사용)"""
    if _port_in_use(start):
        return start
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def _server_ready(port: int) -> bool:
    """Streamlit 서버가 응답하는지 HTTP로 확인."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _start_streamlit(port: int) -> subprocess.Popen | None:
    """Streamlit 서버를 헤드리스로 백그라운드 실행. 이미 떠 있으면 None."""
    if _port_in_use(port) and _server_ready(port):
        return None  # 기존 서버 재사용
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP_SCRIPT),
        "--server.port", str(port),
        "--server.headless", "true",          # 브라우저 자동 오픈 방지
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    # 콘솔 창 숨김 (Windows)
    kwargs: dict = {"cwd": str(HERE.parent)}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(cmd, **kwargs)


def main() -> int:
    try:
        import webview  # pywebview
    except ImportError:
        sys.stderr.write(
            "❌ pywebview가 설치되지 않았습니다.\n"
            "   setup.command(또는 setup.bat)를 다시 실행하거나\n"
            "   '.venv/bin/pip install pywebview'를 실행하세요.\n"
        )
        return 1

    port = _find_free_port(DEFAULT_PORT)
    proc = _start_streamlit(port)

    # 서버가 응답할 때까지 대기 (최대 60초)
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + 60
    while time.time() < deadline:
        if _server_ready(port):
            break
        time.sleep(0.4)
    else:
        sys.stderr.write("❌ 서버가 시간 안에 시작되지 않았습니다.\n")
        if proc:
            proc.terminate()
        return 1

    # 네이티브 창 생성 (주소창 없음)
    webview.create_window(
        APP_TITLE, url,
        width=1280, height=860,
        min_size=(900, 600),
        text_select=True,
    )
    icon = APP_ICON if os.path.exists(APP_ICON) else None
    try:
        webview.start(icon=icon)  # 창이 닫힐 때까지 블록
    finally:
        # 창 닫히면 서버도 종료 (우리가 띄운 경우에만)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
