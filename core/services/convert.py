"""PDF → TXT 변환 (pypdfium2 좌표 추출 + pdftotext 폴백) + TXT 단독 처리."""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import config as cfg

from services.common import PDF_SUB, _nfc, append_log
from services.files import md_dir, txt_dir
from services.pipeline_queue import queue_add
from services import pdfcols, reflowlib

UPLOAD_TMP = cfg.UPLOAD_TMP
DONE_DIR   = cfg.DONE_DIR
FAILED_DIR = cfg.FAILED_DIR

# 텍스트 레이어가 없는 이미지 전용(스캔) 문서를 만났을 때의 신호 문구.
# _do_ocr_only가 이 값을 보고 needs_ocr 플래그를 세워 UI에서 OCR 안내 팝업을 띄운다.
OCR_REQUIRED_MSG = "이미지 전용 문서입니다 — TXT 분리를 위해서는 OCR 사전 처리 작업이 필요합니다."


def _no_window_kwargs() -> dict:
    """Windows에서 콘솔 창이 뜨지 않도록 하는 subprocess 옵션."""
    if sys.platform == "win32":
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0  # SW_HIDE
        return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": _si,
                "stdin": subprocess.DEVNULL}
    return {}


def _pdftotext_fallback(pdf_path: Path) -> str:
    """pypdfium2 추출이 실패했을 때의 폴백 — pdftotext(기본 모드) + reflow."""
    pdftotext = cfg.PDFTOTEXT
    if not pdftotext or not Path(pdftotext).exists():
        return ""
    tmp = Path(tempfile.gettempdir()) / (pdf_path.stem + ".fallback.txt")
    try:
        r = subprocess.run([pdftotext, str(pdf_path), str(tmp)],
                           capture_output=True, text=True, **_no_window_kwargs())
        if r.returncode != 0 or not tmp.exists():
            return ""
        raw = tmp.read_text(encoding="utf-8", errors="ignore")
        return reflowlib.clean_default_text(raw)
    except Exception as e:
        append_log(f"WARN: pdftotext 폴백 실패 ({type(e).__name__}) {str(e)[:120]}")
        return ""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


_LONG_TOKEN_MIN = 26


def _token_health(text: str) -> float:
    """비정상적으로 긴(붙어버린) 토큰의 비율 — 낮을수록 띄어쓰기가 정상(0~1)."""
    toks = text.split()
    if not toks:
        return 1.0
    longs = sum(1 for w in toks if len(w) >= _LONG_TOKEN_MIN)
    return longs / len(toks)


def pdf_to_txt(pdf_path: Path, fast: bool = True) -> tuple[Path | None, Path | None, str, str]:
    """텍스트 레이어가 있는 PDF를 TXT로 변환한다. (2026-07-11 좌표 기반으로 개편)
    1차: pypdfium2 좌표 기반 다단 추출(논문·뉴스레터·한글+영어·N단 처리).
    2차(안전망②): 결과가 비정상이거나 비면 pdftotext 폴백과 비교해 나은 쪽 채택.
    반환: (txt_path, md_path, err, note) — note는 사용자에게 알릴 상황 설명(있을 때)."""
    txt_path = Path(tempfile.gettempdir()) / (pdf_path.stem + ".txt")

    text, skipped = "", 0
    try:
        text, skipped = pdfcols.pdf_to_text(pdf_path)
    except Exception as e:
        append_log(f"WARN: 좌표 추출 실패 → pdftotext 폴백 ({type(e).__name__}) {str(e)[:120]}")

    used_fallback = False
    # 안전망②: 좌표 추출 결과가 비정상(띄어쓰기 붕괴 등)이면 pdftotext와 품질 비교 → 나은 쪽
    if text.strip() and _token_health(text) > 0.03:
        fb = _pdftotext_fallback(pdf_path)
        if fb.strip() and _token_health(fb) < _token_health(text):
            text, used_fallback = fb, True
            append_log(f"좌표 추출 품질 저하 감지 → pdftotext 결과 채택: {pdf_path.name}")

    # 좌표 추출이 아예 비면 폴백
    if not text.strip():
        text = _pdftotext_fallback(pdf_path)
        used_fallback = bool(text.strip())

    # 실질 내용이 없으면 텍스트 레이어가 없는 이미지 전용(스캔) 문서 → OCR 선행 필요
    if not text.strip():
        return None, None, OCR_REQUIRED_MSG, ""

    notes = []
    if skipped:
        notes.append(f"읽지 못한 {skipped}개 페이지를 건너뛰었습니다")
    if used_fallback:
        notes.append("레이아웃이 복잡해 대체 추출 방식을 사용했습니다(다단 정렬이 다를 수 있음)")

    txt_path.write_text(text, encoding="utf-8")
    return txt_path, None, "", " · ".join(notes)


# ─── TXT 단독 처리 (번역·위키 생략) ─────────────────────────

def _do_ocr_only(uf, ws_name: str, fast: bool = False) -> dict:
    """PDF → TXT 변환만 수행. fast=True이면 pdftotext 직접 추출."""
    dest = UPLOAD_TMP / uf.name
    _src = getattr(uf, "_p", None)
    if not (_src and Path(_src).resolve() == dest.resolve()):
        uf.seek(0)
        with open(dest, "wb") as f:
            f.write(uf.read())
    if dest.suffix.lower() != ".pdf":
        txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        final = txt_dir(DONE_DIR, ws_name) / dest.name
        shutil.move(str(dest), str(final))
        append_log(f"TXT 직접 업로드: {final.name}")
        queue_add("tab2_ready", [_nfc(Path(final).stem)])   # → 장별분할 큐
        return {"ok": True, "name": uf.name, "txt_path": str(final), "md_path": "", "error": ""}
    txt_path, md_src, err, note = pdf_to_txt(dest, fast=fast)
    if not txt_path:
        _needs_ocr = (err == OCR_REQUIRED_MSG)
        try: shutil.move(str(dest), str(FAILED_DIR / uf.name))
        except Exception: pass
        append_log(f"{'OCR 필요' if _needs_ocr else 'ERROR: TXT 변환 실패'} — {uf.name}: {err}")
        return {"ok": False, "name": uf.name, "txt_path": "", "md_path": "",
                "error": err, "needs_ocr": _needs_ocr, "note": ""}
    pdf_save_dir2 = cfg.PDF_DIR
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
    append_log(f"TXT 변환 완료: {uf.name} → {Path(final_txt).name}"
               + (f" ({note})" if note else ""))
    queue_add("tab2_ready", [_nfc(Path(final_txt).stem)])   # → 장별분할 큐 등록
    return {"ok": True, "name": uf.name, "txt_path": str(final_txt),
            "md_path": str(final_md) if final_md else "", "error": "", "note": note}
