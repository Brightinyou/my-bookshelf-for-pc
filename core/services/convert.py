"""PDF → TXT 변환 (pdftotext) + TXT 단독 처리."""

import re as _re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import config as cfg

from services.common import PDF_SUB, _nfc, append_log
from services.files import md_dir, txt_dir
from services.pipeline_queue import queue_add

UPLOAD_TMP = cfg.UPLOAD_TMP
DONE_DIR   = cfg.DONE_DIR
FAILED_DIR = cfg.FAILED_DIR


_COLUMN_GAP_RE = _re.compile(r"\S {10,}\S")


def _looks_two_column(text: str) -> bool:
    """pdftotext -layout 출력이 2단 조판(좌우 컬럼 병합)인지 판정. (2026-07-03)
    본문 줄 중간에 넓은 공백(컬럼 간격)이 반복되면 2단으로 본다.
    실측: 2단 저널 -layout = 0.30, 1단 책/재추출 = 0.00~0.01."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 40:
        return False
    gap_n = sum(1 for ln in lines if len(ln) > 60 and _COLUMN_GAP_RE.search(ln))
    return gap_n / len(lines) >= 0.18


def pdf_to_txt(pdf_path: Path, fast: bool = True) -> tuple[Path | None, Path | None, str]:
    """텍스트 레이어가 있는 PDF를 TXT로 변환한다.
    2단 조판이 감지되면 -layout 없이(읽기 순서) 재추출한다."""
    pdftotext = cfg.PDFTOTEXT

    txt_path = Path(tempfile.gettempdir()) / (pdf_path.stem + ".txt")

    # Windows에서 터미널 창이 뜨지 않도록 STARTUPINFO + CREATE_NO_WINDOW 조합 사용
    if sys.platform == "win32":
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0  # SW_HIDE
        _nw = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": _si,
               "stdin": subprocess.DEVNULL}
    else:
        _nw = {}

    if not pdftotext or not Path(pdftotext).exists():
        return None, None, "TXT 변환에 필요한 pdftotext가 없습니다. Windows용 Poppler 설치를 확인하세요."

    r = subprocess.run([pdftotext, "-layout", str(pdf_path), str(txt_path)],
                       capture_output=True, text=True, **_nw)
    if r.returncode != 0:
        return None, None, f"pdftotext 오류 (exit {r.returncode}): {(r.stderr or '').strip() or '알 수 없는 오류'}"

    if not txt_path.exists() or txt_path.stat().st_size == 0:
        return None, None, "텍스트 추출 실패 — 텍스트 레이어가 있는 PDF만 변환할 수 있습니다"

    # 2단 조판 감지 → 읽기 순서(-layout 없이)로 재추출 (2026-07-03)
    # -layout은 좌우 컬럼을 한 줄에 병합해 헤딩 감지·번역 품질을 망가뜨린다.
    try:
        layout_text = txt_path.read_text(encoding="utf-8", errors="ignore")
        if _looks_two_column(layout_text):
            r2 = subprocess.run([pdftotext, str(pdf_path), str(txt_path)],
                                capture_output=True, text=True, **_nw)
            if r2.returncode == 0 and txt_path.exists() and txt_path.stat().st_size > 0:
                append_log(f"2단 조판 감지 → 읽기 순서로 재추출: {pdf_path.name}")
            else:
                txt_path.write_text(layout_text, encoding="utf-8")  # 실패 시 -layout 결과 복원
    except Exception as e:
        append_log(f"WARN: 2단 감지/재추출 실패 ({type(e).__name__}) {str(e)[:120]} — -layout 결과 사용")

    return txt_path, None, ""


# ─── TXT 단독 처리 (번역·위키 생략) ─────────────────────────

def _do_ocr_only(uf, ws_name: str, fast: bool = False) -> dict:
    """PDF → TXT 변환만 수행. fast=True이면 pdftotext 직접 추출."""
    dest = UPLOAD_TMP / uf.name
    _src = getattr(uf, "_p", None)
    if not (_src and Path(_src).resolve() == dest.resolve()):
        uf.seek(0)
        with open(dest, "wb") as f:
            f.write(uf.read())
    done_sub = DONE_DIR / ws_name
    done_sub.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() != ".pdf":
        txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        final = txt_dir(DONE_DIR, ws_name) / dest.name
        shutil.move(str(dest), str(final))
        append_log(f"TXT 직접 업로드: {final.name}")
        queue_add("tab2_ready", [_nfc(Path(final).stem)])   # → 장별분할 큐
        return {"ok": True, "name": uf.name, "txt_path": str(final), "md_path": "", "error": ""}
    txt_path, md_src, err = pdf_to_txt(dest, fast=fast)
    if not txt_path:
        try: shutil.move(str(dest), str(FAILED_DIR / uf.name))
        except Exception: pass
        append_log(f"ERROR: TXT 변환 실패 — {uf.name}: {err}")
        return {"ok": False, "name": uf.name, "txt_path": "", "md_path": "", "error": err}
    pdf_save_dir2 = done_sub / PDF_SUB
    pdf_save_dir2.mkdir(parents=True, exist_ok=True)
    final_pdf = pdf_save_dir2 / uf.name
    shutil.move(str(dest), str(final_pdf))
    txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
    final_txt = txt_dir(DONE_DIR, ws_name) / txt_path.name   # 항상 1_txt/에 저장
    shutil.move(str(txt_path), str(final_txt))
    if md_src and md_src.exists():
        md_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        final_md = md_dir(DONE_DIR, ws_name) / md_src.name
        shutil.move(str(md_src), str(final_md))
    else:
        final_md = None
    append_log(f"TXT 변환 완료: {uf.name} → {Path(final_txt).name}")
    queue_add("tab2_ready", [_nfc(Path(final_txt).stem)])   # → 장별분할 큐 등록
    return {"ok": True, "name": uf.name, "txt_path": str(final_txt),
            "md_path": str(final_md) if final_md else "", "error": ""}
