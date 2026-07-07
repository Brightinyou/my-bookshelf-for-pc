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
    "old_done": "...", "old_translated": "...",
    "upload_tmp": "...  (업로드/재시도 대기 폴더 — 내장 디스크가 작으면 외장으로)"
  },
  "files": {
    "log_file": "...", "results_file": "...", "gemini_done": "..."
  },
  "binaries": { "pdftotext": "..." },
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
# 2026-07-07 v0.9.0 폴더 재구성: 앱 단계와 1:1 대응하는 단일 트리.
#   0_업로드대기 → (TXT변환) → 2_변환TXT → (장분할) → 3_챕터 → (번역·요약) → 위키
#   원본 PDF는 1_원본PDF에 보관. 숫자 접두 = 작업 순서.
# 기존 데이터는 services/migrate.py가 첫 실행 때 자동 이동한다.
BASE_DIR = _p(_cfg.get("base_dir") or "~/Documents/My Bookshelf")

# MYBOOKSHELF_WIKI_DIR 환경변수가 있으면 그 보관함으로 출력(업로드별 선택, 2026-06-11).
_env_wiki = os.environ.get("MYBOOKSHELF_WIKI_DIR", "").strip()
WIKI_DIR      = _p(_env_wiki) if _env_wiki else _dir("wiki", BASE_DIR / "wiki")


def _folder_lang() -> str:
    """폴더명 언어 — config.json "folder_lang"이 있으면 그것으로 고정(pin).
    없으면 UI 언어 규칙과 동일하게 해석: env → config "lang" → app_lang.txt → ko.
    첫 마이그레이션 때 migrate.py가 folder_lang을 기록해 이후 언어를 바꿔도
    폴더가 움직이지 않게 한다."""
    v = str(_cfg.get("folder_lang", "")).strip().lower()
    if v in ("ko", "en"):
        return v
    v = os.environ.get("MYBOOKSHELF_LANG", "").strip().lower()
    if v in ("ko", "en"):
        return v
    v = str(_cfg.get("lang", "")).strip().lower()
    if v in ("ko", "en"):
        return v
    try:
        f = _HERE_EARLY.parent / "app_lang.txt"
        if f.exists():
            v = f.read_text(encoding="utf-8", errors="ignore").strip().lower()
            if v in ("ko", "en"):
                return v
    except Exception:
        pass
    return "ko"


_HERE_EARLY = Path(__file__).resolve().parent
FOLDER_LANG = _folder_lang()
_FN = {   # 폴더 표시명 — 한국어/영어 (2026-07-07)
    "ko": {"upload": "0_업로드대기", "pdf": "1_원본PDF", "txt": "2_변환TXT",
           "chapters": "3_챕터", "failed": "실패", "logs": "로그",
           "legacy": "_구버전보관", "txt_done": "완료"},
    "en": {"upload": "0_Inbox", "pdf": "1_PDF_Originals", "txt": "2_Converted_TXT",
           "chapters": "3_Chapters", "failed": "Failed", "logs": "Logs",
           "legacy": "_Legacy_Files", "txt_done": "Done"},
}[FOLDER_LANG]

UPLOAD_TMP    = _dir("upload_tmp", BASE_DIR / _FN["upload"])    # 처리전 투입함
PDF_DIR       = _dir("pdf",      BASE_DIR / _FN["pdf"])         # 원본 보관
TXT_DIR       = _dir("txt",      BASE_DIR / _FN["txt"])         # 변환 TXT
TXT_ARCHIVE_DIR = TXT_DIR / _FN["txt_done"]                     # 분할 끝난 원본 보관
CHAPTERS_DIR  = _dir("chapters", BASE_DIR / _FN["chapters"])    # 챕터·번역·요약 작업장
FAILED_DIR    = _dir("failed",   BASE_DIR / _FN["failed"])
LOG_DIR       = _dir("logs",     BASE_DIR / _FN["logs"])
PAUSE_DIR     = _dir("pause",    BASE_DIR / ".pause")
WIKI_LOG_DIR  = _dir("wiki_log", LOG_DIR)          # gemini_wiki_YYYYMMDD.log 위치
QUEUE_FILE    = BASE_DIR / ".pipeline_queue.json"
LEGACY_KEEP   = BASE_DIR / _FN["legacy"]            # 마이그레이션이 옮겨두는 옛 산출물

# 옛 레이아웃 — 마이그레이션·fallback 용. 없으면 호출부 .exists() 가드로 무시.
DONE_DIR      = _dir("done",   BASE_DIR / "done")
RAW_DIR       = _dir("raw",    BASE_DIR / "raw")
PROCESSED_DIR = RAW_DIR / "processed"
OLD_DONE_DIR       = _dir("old_done",       BASE_DIR / "_legacy" / "done")
OLD_TRANSLATED_DIR = _dir("old_translated", BASE_DIR / "_legacy" / "translated")
BILINGUAL_DIR = TXT_DIR / "bilingual"               # 전체실행 모드의 대역 번역 산출물

LOG_FILE         = _file("log_file",     LOG_DIR / "upload.log")
RESULTS_FILE     = _file("results_file", LOG_DIR / "pipeline_results.json")
GEMINI_DONE_FILE = _file("gemini_done",  LOG_DIR / "gemini_done.txt")


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


_PARENT = _HERE.parent   # core/ → 상위 폴더 (.venv가 여기 있을 수도 있음)
# 번들 poppler (2026-07-03): 인스톨러가 {app}\poppler로 설치, 개발 레포는 vendor\poppler
PDFTOTEXT = find_binary("pdftotext", extra=(
    str(_PARENT / "poppler" / "Library" / "bin" / "pdftotext.exe"),
    str(_PARENT / "vendor" / "poppler" / "Library" / "bin" / "pdftotext.exe"),
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
