"""ocr_windows.py — Windows 스캔 PDF OCR 라우터 (v0.4.3)

WinRT-지원 언어 → Windows.Media.Ocr (빠르고 정확, OS 내장)
WinRT-미지원 언어 → Docling + Tesseract (다국어, 레이아웃 보존)

사용:
    from ocr_windows import is_scanned, ocr_windows_scanned
"""
from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── WinRT가 지원하는 언어 코드 집합 (our LANGS keys) ─────────────────────────
WINRT_LANGS: set[str] = {
    "ko", "en",
    "zh", "zh-tw", "ja",
    "ar", "fa", "ur", "he",
    "hi", "bn", "ne",
    "th",
    "ru",
    "de", "fr", "es", "pt", "nl",
    "tr",
    "vi", "id", "ms",
}

# ── Tesseract tessdata 언어 코드 매핑 ─────────────────────────────────────────
TESS_LANG_MAP: dict[str, str] = {
    "ko": "kor", "en": "eng",
    "zh": "chi_sim", "zh-tw": "chi_tra", "ja": "jpn",
    "mn": "mon",
    "th": "tha", "km": "khm", "vi": "vie",
    "id": "ind", "ms": "msa", "tl": "tgl",
    "my": "mya", "lo": "lao",
    "hi": "hin", "ne": "nep", "bn": "ben",
    "si": "sin", "ur": "urd",
    "ru": "rus", "kk": "kaz", "uz": "uzb", "ky": "kir", "tg": "tgk",
    "ar": "ara", "fa": "fas", "tr": "tur", "he": "heb", "ku": "kur",
    "am": "amh", "ti": "tir", "sw": "swa",
    "ha": "hau", "yo": "yor", "ig": "ibo", "so": "som", "mg": "mlg",
    "de": "deu", "fr": "fra", "es": "spa", "pt": "por", "nl": "nld",
}

# ── WinRT BCP-47 언어 태그 매핑 ──────────────────────────────────────────────
WINRT_LANG_MAP: dict[str, str] = {
    "ko": "ko-KR", "en": "en-US",
    "zh": "zh-CN", "zh-tw": "zh-TW", "ja": "ja-JP",
    "ar": "ar-SA", "fa": "fa-IR", "ur": "ur-PK", "he": "he-IL",
    "hi": "hi-IN", "bn": "bn-BD", "ne": "ne-NP",
    "th": "th-TH",
    "ru": "ru-RU",
    "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "pt": "pt-BR", "nl": "nl-NL",
    "tr": "tr-TR",
    "vi": "vi-VN", "id": "id-ID", "ms": "ms-MY",
}


def is_scanned(pdf_path: Path, pdftotext_bin: str | None = None) -> bool:
    """PDF 텍스트 레이어 유무 확인. 100자 미만이면 스캔으로 판단."""
    if not pdftotext_bin or not Path(pdftotext_bin).exists():
        return True  # 확인 불가 → 스캔으로 간주
    try:
        r = subprocess.run(
            [pdftotext_bin, "-l", "5", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        return len((r.stdout or "").strip()) < 100
    except Exception:
        return True


def _winrt_available(lang_code: str) -> bool:
    """WinRT OCR 패키지가 설치되어 있고 해당 언어를 지원하는지 확인."""
    if sys.platform != "win32":
        return False
    if lang_code not in WINRT_LANG_MAP:
        return False
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        bcp47 = WINRT_LANG_MAP[lang_code]
        lang = Language(bcp47)
        return OcrEngine.try_create_from_language(lang) is not None
    except Exception:
        return False


def _tesseract_bin() -> str | None:
    """Tesseract 바이너리 경로 탐지."""
    import shutil
    p = shutil.which("tesseract")
    if p:
        return p
    for cand in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if Path(cand).exists():
            return cand
    return None


# ── WinRT 직접 OCR ────────────────────────────────────────────────────────────

async def _winrt_page_async(pil_img, bcp47: str) -> str:
    """PIL 이미지 한 페이지를 WinRT OCR로 인식."""
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    from winrt.windows.globalization import Language

    # PIL → BMP bytes → WinRT InMemoryRandomAccessStream
    buf = io.BytesIO()
    pil_img.save(buf, format="BMP")
    raw = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(list(raw))
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    lang = Language(bcp47)
    engine = OcrEngine.try_create_from_language(lang)
    if engine is None:
        raise RuntimeError(f"WinRT OCR: 언어 팩 없음 ({bcp47})")
    result = await engine.recognize_async(bitmap)
    return result.text


async def _winrt_pdf_async(pdf_path: Path, lang_code: str) -> str:
    """PDF 전체를 WinRT OCR로 변환 → 텍스트 반환."""
    import pypdfium2 as pdfium
    bcp47 = WINRT_LANG_MAP[lang_code]
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages = []
    for i in range(len(pdf)):
        page = pdf[i]
        bitmap = page.render(scale=2.0)   # ~144dpi
        pil_img = bitmap.to_pil()
        text = await _winrt_page_async(pil_img, bcp47)
        pages.append(text)
    return "\n\n".join(pages)


def _ocr_winrt(pdf_path: Path, lang_code: str) -> str:
    """동기 래퍼: WinRT async OCR 실행."""
    return asyncio.run(_winrt_pdf_async(pdf_path, lang_code))


# ── 메인 라우터 ───────────────────────────────────────────────────────────────

def ocr_windows_scanned(
    pdf_path: Path,
    lang_code: str,
    docling_bin: str | None,
    ocr_langs_other: str,
) -> tuple[str | None, str]:
    """스캔 PDF OCR 라우팅.

    Returns:
        (text, error_msg) — text=None 이면 실패
    """
    # ── ① WinRT (고품질, OS 내장) ─────────────────────────────────────────────
    if lang_code in WINRT_LANGS and _winrt_available(lang_code):
        try:
            text = _ocr_winrt(pdf_path, lang_code)
            if text and len(text.strip()) > 50:
                return text, ""
        except Exception as e:
            pass   # WinRT 실패 시 Tesseract로 폴백

    # ── ② Tesseract via Docling (다국어, 레이아웃 보존) ───────────────────────
    if docling_bin and Path(docling_bin).exists() and _tesseract_bin():
        tess_lang = TESS_LANG_MAP.get(lang_code, "eng")
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            stale = tmp_dir / (pdf_path.stem + ".md")
            r = subprocess.run(
                [docling_bin, str(pdf_path),
                 "--to", "md",
                 "--image-export-mode", "placeholder",
                 "--ocr-engine", "tesseract",
                 "--ocr-lang", tess_lang,
                 "--pdf-backend", "pypdfium2",
                 "--output", str(tmp_dir)],
                capture_output=True, text=True, timeout=3600,
            )
            cand = tmp_dir / (pdf_path.stem + ".md")
            if cand.exists() and cand.stat().st_size > 0:
                import re as _re
                md = cand.read_text(encoding="utf-8", errors="ignore")
                md = _re.sub(r"!\[Image\]\([^)]*\)\s*", "", md)
                return md, ""
            return None, f"Docling+Tesseract 실패 (exit {r.returncode}): {(r.stderr or '')[-200:]}"
        except subprocess.TimeoutExpired:
            return None, "Docling+Tesseract 타임아웃(3600초)"
        except Exception as e:
            return None, f"Docling+Tesseract 오류: {type(e).__name__} {str(e)[:200]}"
        finally:
            import shutil as _sh
            _sh.rmtree(tmp_dir, ignore_errors=True)

    # ── ③ EasyOCR via Docling (폴백) ─────────────────────────────────────────
    return None, "WinRT·Tesseract 모두 사용 불가 — EasyOCR 폴백 필요"


def tesseract_status() -> dict:
    """설정 탭 표시용 Tesseract 상태."""
    bin_path = _tesseract_bin()
    result = {"available": bool(bin_path), "path": bin_path or ""}
    if bin_path:
        try:
            r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
            ver_line = (r.stdout or r.stderr or "").split("\n")[0]
            result["version"] = ver_line.strip()
        except Exception:
            result["version"] = "확인 불가"
    return result


def winrt_status(lang_code: str = "ko") -> dict:
    """설정 탭 표시용 WinRT 상태."""
    if sys.platform != "win32":
        return {"available": False, "reason": "Windows 전용"}
    try:
        import importlib
        importlib.import_module("winrt.windows.media.ocr")
        pkg_ok = True
    except ImportError:
        return {"available": False, "reason": "winrt 패키지 미설치 (pip install winrt-Windows.Media.Ocr)"}
    bcp47 = WINRT_LANG_MAP.get(lang_code, "")
    if not bcp47:
        return {"available": False, "reason": f"{lang_code} 언어는 WinRT 미지원"}
    avail = _winrt_available(lang_code)
    return {
        "available": avail,
        "reason": "" if avail else f"{bcp47} 언어 팩 없음 — Windows 설정→언어 추가",
    }
