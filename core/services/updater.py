# -*- coding: utf-8 -*-
"""앱 내 업데이트 (반자동, Windows). GitHub Releases 기반, 별도 서버 불필요.

흐름(가드레일 포함):
  1) 감지: releases/latest의 tag_name을 APP_VERSION과 비교
  2) 다운로드: Setup.exe를 임시폴더에 받고 크기·PE헤더 검증
  3) 설치: 분리형 헬퍼(PowerShell)가 앱 종료를 기다린 뒤(백업으로 강제종료)
     Setup.exe /SILENT 실행 → 앱 재실행
  4) 어느 단계든 실패하면 호출부가 '릴리스 페이지 열기'(안내형 A)로 폴백
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser
from pathlib import Path

import config as cfg
from services.common import append_log

try:
    from version import APP_VERSION
except Exception:
    APP_VERSION = "v0.0.0"

REPO = "Brightinyou/my-bookshelf-for-pc"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse_ver(s: str) -> tuple:
    s = (s or "").strip().lstrip("vV")
    parts = []
    for chunk in s.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _app_root() -> Path:
    """설치 루트(=코드가 있는 곳). 설치본은 {localappdata}\\My Bookshelf."""
    return Path(cfg.__file__).resolve().parent.parent


def check_for_update(timeout: int = 4) -> dict | None:
    """새 버전이 있으면 정보 dict, 없거나(=최신) 오류면 None. 네트워크 실패는 조용히 무시."""
    if sys.platform != "win32":
        return None
    try:
        req = urllib.request.Request(
            API_LATEST,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "MyBookshelf-Updater"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception as e:
        append_log(f"업데이트 확인 실패(무시): {type(e).__name__} {str(e)[:80]}")
        return None
    tag = data.get("tag_name", "")
    if _parse_ver(tag) <= _parse_ver(APP_VERSION):
        return None
    asset_url = ""
    for a in data.get("assets", []):
        if (a.get("name", "").lower() == "setup.exe"):
            asset_url = a.get("browser_download_url", "")
            break
    return {
        "available": True,
        "current": APP_VERSION,
        "latest": tag,
        "notes": (data.get("body") or "").strip(),
        "page_url": data.get("html_url", ""),
        "asset_url": asset_url,
    }


def download_installer(asset_url: str, progress_cb=None) -> tuple[Path | None, str]:
    """Setup.exe를 임시폴더에 받고 검증. 반환: (경로 또는 None, 오류문구)."""
    if not asset_url:
        return None, "설치 파일 주소를 찾을 수 없습니다."
    dest = Path(tempfile.gettempdir()) / "MyBookshelf-Setup-update.exe"
    try:
        req = urllib.request.Request(asset_url, headers={"User-Agent": "MyBookshelf-Updater"})
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(1.0, got / total))
    except Exception as e:
        return None, f"다운로드 실패: {type(e).__name__} {str(e)[:80]}"
    # 무결성 검증: 크기 + PE 헤더(MZ). (브라우저 경유가 아니라 MOTW가 없어 SmartScreen 위험도 낮음)
    try:
        if dest.stat().st_size < 200_000:
            return None, "다운로드가 불완전합니다(파일 크기 이상)."
        with open(dest, "rb") as f:
            if f.read(2) != b"MZ":
                return None, "받은 파일이 올바른 설치 파일이 아닙니다."
    except Exception as e:
        return None, f"검증 실패: {str(e)[:80]}"
    return dest, ""


_HELPER_PS1 = r"""
param([string]$Root, [string]$Setup, [string]$Relaunch)
$ErrorActionPreference = 'SilentlyContinue'
function AppProcs {
  Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($Root.ToLower()) -and $_.Name -match 'python'
  }
}
# 1) 앱(설치 폴더의 python) 종료 대기 — 파일 잠금 해제 목적
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline -and (AppProcs)) { Start-Sleep -Milliseconds 400 }
# 2) 백업: 남아 있으면 강제 종료
foreach ($p in AppProcs) { try { Stop-Process -Id $p.ProcessId -Force } catch {} }
Start-Sleep -Seconds 2
# 3) 설치 (per-user, UAC 없음; /SILENT = 진행바만, 클릭 불필요)
try { Start-Process -FilePath $Setup -ArgumentList '/SILENT','/NORESTART' -Wait } catch {}
Start-Sleep -Seconds 1
# 4) 재실행
if (Test-Path $Relaunch) { Start-Process -FilePath $Relaunch }
"""


def _write_helper() -> Path:
    helper = Path(tempfile.gettempdir()) / "mybookshelf_update_helper.ps1"
    helper.write_text(_HELPER_PS1, encoding="utf-8")
    return helper


def launch_helper_and_exit(installer_path: Path) -> bool:
    """대기/설치/재실행 헬퍼를 분리 실행하고 앱을 종료한다.
    성공 시 곧 프로세스가 종료된다. 실행 자체가 실패하면 False(→ 호출부는 A로 폴백)."""
    root = _app_root()
    relaunch = root / "MyBookshelf.exe"
    if not relaunch.exists():
        relaunch = root / "start-app.vbs"
    try:
        helper = _write_helper()
        DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_GROUP|NO_WINDOW
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-File", str(helper),
             "-Root", str(root), "-Setup", str(installer_path), "-Relaunch", str(relaunch)],
            creationflags=DETACHED, close_fds=True,
        )
    except Exception as e:
        append_log(f"업데이트 헬퍼 실행 실패: {type(e).__name__} {str(e)[:80]}")
        return False

    # 앱(창=desktop.py 및 자신=streamlit)을 정리하고 종료 → 헬퍼가 즉시 설치 진행
    _terminate_parent_tree()
    import threading
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return True


def _terminate_parent_tree() -> None:
    """창 런처(desktop.py의 pythonw 등) 부모 python 프로세스를 정리한다(헬퍼 대기 단축)."""
    try:
        import psutil
        me = psutil.Process()
        for p in me.parents():
            try:
                if "python" in p.name().lower():
                    p.terminate()
            except Exception:
                pass
    except Exception:
        pass


def open_release_page(url: str) -> None:
    """안내형(A) 폴백 — 릴리스 페이지를 기본 브라우저로 연다."""
    try:
        webbrowser.open(url or f"https://github.com/{REPO}/releases/latest")
    except Exception:
        pass
