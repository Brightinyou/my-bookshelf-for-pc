#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""My Bookshelf — 기계 의존 설정의 단일 출처 (2026-06-10 신설).

경로·바이너리·분류 폴더를 모두 여기서 해석한다.
기본값은 `~/Documents/My Bookshelf` 아래 표준 구조이고,
`~/.config/mybookshelf/config.json` 이 있으면 적힌 키만 덮어쓴다.

config.json 예 (모든 키 선택 사항):
{
  "base_dir": "~/Documents/My Bookshelf",
  "dirs": {
    "raw": "...", "wiki": "...", "done": "...", "failed": "...",
    "pause": "...", "wiki_log": "...",
    "old_done": "...", "old_translated": "..."
  },
  "files": {
    "log_file": "...", "results_file": "...", "gemini_done": "..."
  },
  "binaries": { "docling": "...", "pdftotext": "..." },
  "workspaces": ["My Bookshelf"]
}
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

CONFIG_DIR  = Path.home() / ".config" / "mybookshelf"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_cfg   = _load()
_dirs  = _cfg.get("dirs", {})
_files = _cfg.get("files", {})
_bins  = _cfg.get("binaries", {})


def _p(v) -> Path:
    return Path(v).expanduser()


def _dir(key: str, default: Path) -> Path:
    return _p(_dirs[key]) if _dirs.get(key) else default


def _file(key: str, default: Path) -> Path:
    return _p(_files[key]) if _files.get(key) else default


# ── 경로 ─────────────────────────────────────────────────
BASE_DIR = _p(_cfg.get("base_dir") or "~/Documents/My Bookshelf")

RAW_DIR       = _dir("raw",    BASE_DIR / "raw")
PROCESSED_DIR = RAW_DIR / "processed"
# MYBOOKSHELF_WIKI_DIR 환경변수가 있으면 그 금고로 출력(업로드별 금고 선택, 2026-06-11).
# 앱이 위키 생성기를 띄울 때 선택 금고를 이 변수로 전달한다.
_env_wiki = os.environ.get("MYBOOKSHELF_WIKI_DIR", "").strip()
WIKI_DIR      = _p(_env_wiki) if _env_wiki else _dir("wiki", BASE_DIR / "wiki")
DONE_DIR      = _dir("done",   BASE_DIR / "done")
FAILED_DIR    = _dir("failed", BASE_DIR / "failed")
PAUSE_DIR     = _dir("pause",  BASE_DIR / ".pause")
LOG_DIR       = _dir("logs",   BASE_DIR / "logs")
WIKI_LOG_DIR  = _dir("wiki_log", LOG_DIR)          # gemini_wiki_YYYYMMDD.log 위치

# 옛 레이아웃 fallback — 없으면 호출부 .exists() 가드로 그냥 무시된다.
OLD_DONE_DIR       = _dir("old_done",       BASE_DIR / "_legacy" / "done")
OLD_TRANSLATED_DIR = _dir("old_translated", BASE_DIR / "_legacy" / "translated")

LOG_FILE         = _file("log_file",     LOG_DIR / "upload.log")
RESULTS_FILE     = _file("results_file", LOG_DIR / "pipeline_results.json")
GEMINI_DONE_FILE = _file("gemini_done",  LOG_DIR / "gemini_done.txt")

# 업로드 대기 파일은 재시작 후에도 다시 찾아야 하므로 맥은 기존 /tmp 경로 유지(호환),
# 윈도우는 %TEMP% 사용.
UPLOAD_TMP = (Path("/tmp") if sys.platform == "darwin" else Path(tempfile.gettempdir())) \
             / "pipeline_uploads"


# ── 분류 폴더(워크스페이스) ───────────────────────────────
# 첫 항목이 기본 보관 폴더. 노트 분류 자체는 Gemini 카테고리가 담당.
WORKSPACES: list[str] = [str(w) for w in _cfg.get("workspaces", []) if str(w).strip()] \
                        or ["My Bookshelf"]


# ── 바이너리 탐지 ─────────────────────────────────────────
_HERE = Path(__file__).resolve().parent


def find_binary(name: str, extra: tuple = ()) -> str | None:
    """config.json binaries.<name> → shutil.which → 후보 경로 순으로 탐지.
    AS맥=/opt/homebrew, Intel맥=/usr/local 모두 커버."""
    cfgd = _bins.get(name)
    if cfgd and _p(cfgd).exists():
        return str(_p(cfgd))
    p = shutil.which(name)
    if p:
        return p
    for cand in tuple(extra) + (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if Path(cand).exists():
            return str(cand)
    return None


PDFTOTEXT = find_binary("pdftotext")
DOCLING   = find_binary("docling", extra=(
    str(_HERE / ".venv" / "bin" / "docling"),              # 배포본: 앱 폴더 venv (맥)
    str(_HERE / ".venv" / "Scripts" / "docling.exe"),      # 배포본: 앱 폴더 venv (윈도우)
    str(BASE_DIR / ".venv" / "bin" / "docling"),           # 옛 레이아웃 호환
))
PYTHON    = sys.executable   # 보조 스크립트는 앱과 같은 인터프리터로 실행


# ── 동반 스크립트 위치 ────────────────────────────────────
def find_script(name: str) -> Path:
    """앱 폴더 → ~/.local/bin 순으로 탐색. 못 찾아도 Path를 돌려줘
    호출부의 .exists() 가드가 그대로 작동하게 한다."""
    for d in (_HERE, Path.home() / ".local" / "bin"):
        p = d / name
        if p.exists():
            return p
    return _HERE / name
