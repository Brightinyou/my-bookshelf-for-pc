"""공용 유틸 — 로그, 알림, 원자적 저장, 일시정지 플래그, 공통 상수."""

import json
import os
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path

import config as cfg

# done/<ws>/ 하위 산출물 폴더명 — 텍스트 처리 순서대로 번호 접두 (2026-06-09).
#   1_txt(②변환 TXT, Gemini 입력) → 2_md(③MD, 장 구조) → 3_translated(④번역)
TXT_SUB   = "1_txt"
MD_SUB    = "2_md"
TRANS_SUB = "3_translated"
PDF_SUB   = "pdf"          # 원본 PDF 보관 폴더

DEFAULT_WS = "My Bookshelf"   # 단일 기본 폴더

LOG_FILE     = cfg.LOG_FILE
RESULTS_FILE = cfg.RESULTS_FILE


def _nfc(s: str) -> str:
    """맥 파일명은 NFD라 비교 전 NFC 정규화 필수 (한글)."""
    return unicodedata.normalize("NFC", s)


def append_log(msg: str):   # encoding 미지정이면 윈도우 cp949 → 이모지에서 크래시 (2026-06-11)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{ts}] {msg}\n")


def read_log(n: int = 20) -> list:
    if not LOG_FILE.exists():
        return []
    return LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]


def load_pipeline_results() -> list:
    if not RESULTS_FILE.exists():
        return []
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pipeline_results(results: list):
    try:
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_FILE.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _save_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def open_path(p: Path, reveal: bool = False):
    """파일을 OS 기본 앱으로 열기. reveal=폴더에서 선택 표시.
    (2026-06-11 윈도우 수정 — 'open'은 맥 전용)"""
    try:
        if reveal:
            # 리스트로 넘기면 인자 전체가 따옴표로 감싸여 explorer가 무시하고
            # 문서 폴더를 열어버림 — 경로만 따옴표한 문자열로 직접 구성 (2026-06-11)
            subprocess.run(f'explorer /select,"{p}"')
        else:
            os.startfile(str(p))
    except Exception as e:
        append_log(f"WARN: 파일 열기 실패 ({type(e).__name__}) {str(e)[:120]}")


def notify(msg: str, title: str = "My Bookshelf"):
    return


# ─── 재시도 대기 파일 wrapper (file_uploader 인터페이스 모방, 2026-05-19) ──
class _PathAsUpload:
    """Path를 file_uploader 결과와 같은 인터페이스로 감싸기."""
    def __init__(self, p):
        self._p = Path(p)
        self.name = self._p.name
    def read(self) -> bytes:
        return self._p.read_bytes()
    def seek(self, pos: int):
        pass   # read()가 매번 디스크에서 새로 읽음 — UploadedFile.seek 호환용 (2026-06-11)


# ─── 일시정지 플래그 (워커 thread ↔ 메인 UI 통신, 2026-05-19) ──────────
PAUSE_DIR = cfg.PAUSE_DIR
PAUSE_DIR.mkdir(parents=True, exist_ok=True)


def pause_flag_path(stem: str) -> Path:
    """파일명 안전화 — 한글·공백 그대로 둠 (Path가 처리)."""
    return PAUSE_DIR / f"{stem}.pause"


def is_paused(stem: str) -> bool:
    return pause_flag_path(stem).exists()


def set_paused(stem: str, paused: bool):
    p = pause_flag_path(stem)
    if paused:
        p.touch()
    else:
        if p.exists():
            try: p.unlink()
            except Exception: pass
