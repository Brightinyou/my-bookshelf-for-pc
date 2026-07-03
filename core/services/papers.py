"""논문 출처(URL/DOI/arXiv) 다운로드 → TXT 준비 → 번역/큐 등록."""

import re as _re
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import config as cfg

from services.common import DEFAULT_WS, PDF_SUB, _nfc
from services.convert import pdf_to_txt
from services.files import txt_dir
from services.pipeline_queue import queue_add
from services.translate import translate_one_chapter

DONE_DIR = cfg.DONE_DIR


def _safe_source_stem(source: str, fallback: str = "paper") -> str:
    stem = _re.sub(r"^https?://", "", source.strip(), flags=_re.I)
    stem = _re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", stem).strip("._-")
    return (stem[:90] or fallback)


def _paper_source_candidates(source: str) -> list[str]:
    """논문 출처(URL/DOI/arXiv)에서 다운로드를 시도할 후보 URL 목록."""
    from urllib.parse import quote

    src = source.strip()
    if not src:
        return []
    candidates: list[str] = []
    arxiv = _re.search(r"(?:arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?", src, _re.I)
    if arxiv:
        candidates.append(f"https://arxiv.org/pdf/{arxiv.group(1)}")
    if src.lower().startswith(("http://", "https://")):
        candidates.append(src)
    elif src.lower().startswith("doi:"):
        candidates.append("https://doi.org/" + quote(src[4:].strip(), safe="/.()"))
    elif src.startswith("10.") and "/" in src:
        candidates.append("https://doi.org/" + quote(src, safe="/.()"))
    return list(dict.fromkeys(candidates))


def _response_filename(resp, fallback_stem: str, suffix: str) -> str:
    from urllib.parse import unquote, urlparse

    cd = resp.headers.get("Content-Disposition", "")
    m = _re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, _re.I)
    if m:
        name = unquote(m.group(1)).strip()
    else:
        name = Path(urlparse(resp.geturl()).path).name or fallback_stem + suffix
    if not Path(name).suffix:
        name += suffix
    return _re.sub(r'[/\\:*?"<>|]', "_", name)


def _extract_pdf_link_from_html(html: str, base_url: str) -> str | None:
    from urllib.parse import urljoin

    patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    ]
    for pat in patterns:
        m = _re.search(pat, html, _re.I)
        if m:
            return urljoin(base_url, m.group(1).replace("&amp;", "&"))
    return None


def _download_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def download_paper_source(source: str) -> tuple[bool, Path | None, str]:
    """논문 출처가 실제 다운로드 가능한 PDF/TXT인지 확인하고 임시 파일로 저장."""
    candidates = _paper_source_candidates(source)
    if not candidates:
        return False, None, "URL/DOI/arXiv 형식이 아닙니다"
    fallback_stem = _safe_source_stem(source)
    headers = {
        "User-Agent": "MyBookshelf/0.6 (+https://localhost)",
        "Accept": "application/pdf,text/plain,text/html;q=0.8,*/*;q=0.5",
    }
    last_reason = "다운로드 가능한 PDF/TXT 링크를 찾지 못했습니다"
    seen: set[str] = set()
    ssl_context = _download_ssl_context()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20, context=ssl_context) as resp:
                data = resp.read(25 * 1024 * 1024 + 1)
                if len(data) > 25 * 1024 * 1024:
                    return False, None, "파일이 25MB를 초과합니다"
                ctype = (resp.headers.get("Content-Type") or "").lower()
                final_url = resp.geturl()
                if data.startswith(b"%PDF") or "application/pdf" in ctype:
                    name = _response_filename(resp, fallback_stem, ".pdf")
                    out = Path(tempfile.gettempdir()) / name
                    out.write_bytes(data)
                    return True, out, ""
                if "text/plain" in ctype:
                    name = _response_filename(resp, fallback_stem, ".txt")
                    out = Path(tempfile.gettempdir()) / name
                    out.write_bytes(data)
                    return True, out, ""
                if "html" in ctype or data[:512].lstrip().lower().startswith(b"<!doctype html") or b"<html" in data[:2048].lower():
                    html = data.decode("utf-8", errors="ignore")
                    pdf_url = _extract_pdf_link_from_html(html, final_url)
                    if pdf_url and pdf_url not in seen:
                        candidates.append(pdf_url)
                    else:
                        last_reason = "페이지는 열리지만 PDF 다운로드 링크를 찾지 못했습니다"
                else:
                    last_reason = f"지원하지 않는 응답 형식입니다: {ctype or '알 수 없음'}"
        except urllib.error.HTTPError as e:
            last_reason = f"서버가 HTTP {e.code}로 거부했습니다"
        except urllib.error.URLError as e:
            last_reason = f"네트워크 오류: {getattr(e, 'reason', e)}"
        except Exception as e:
            last_reason = f"{type(e).__name__}: {str(e)[:160]}"
    return False, None, last_reason


def translate_downloaded_paper(source_file: Path, engine: str, progress_cb=None) -> tuple[bool, str]:
    """다운로드한 논문 파일을 TXT로 준비한 뒤 한국어 번역본을 저장."""
    try:
        txt_dir(DONE_DIR, DEFAULT_WS).mkdir(parents=True, exist_ok=True)
        pdf_dir = DONE_DIR / DEFAULT_WS / PDF_SUB
        pdf_dir.mkdir(parents=True, exist_ok=True)
        if source_file.suffix.lower() == ".pdf":
            txt_path, _md, err = pdf_to_txt(source_file)
            if not txt_path:
                return False, f"PDF 텍스트 추출 실패: {err}"
            final_pdf = pdf_dir / source_file.name
            shutil.copy2(str(source_file), str(final_pdf))
            final_txt = txt_dir(DONE_DIR, DEFAULT_WS) / (source_file.stem + ".txt")
            shutil.move(str(txt_path), str(final_txt))
        else:
            final_txt = txt_dir(DONE_DIR, DEFAULT_WS) / source_file.name
            shutil.copy2(str(source_file), str(final_txt))
        ok, msg = translate_one_chapter(final_txt, engine, progress_cb=progress_cb)
        if ok:
            queue_add("tab4_ready", [str(final_txt.relative_to(DONE_DIR))])
            return True, f"{msg} → {final_txt.with_name(final_txt.stem + '_ko.txt').name}"
        return False, msg
    except Exception as e:
        return False, str(e)[:200]


def prepare_downloaded_paper_source(source_file: Path) -> tuple[bool, Path | None, Path | None, str]:
    """다운로드한 논문 파일을 1_txt에 저장하고 장별분할 대기열에 등록.
    반환: (ok, final_txt, final_pdf, msg) — final_pdf는 원본이 PDF일 때 보관 경로."""
    try:
        out_txt_dir = txt_dir(DONE_DIR, DEFAULT_WS)
        out_txt_dir.mkdir(parents=True, exist_ok=True)
        pdf_out_dir = DONE_DIR / DEFAULT_WS / PDF_SUB
        pdf_out_dir.mkdir(parents=True, exist_ok=True)
        final_pdf: Path | None = None
        if source_file.suffix.lower() == ".pdf":
            txt_path, _md, err = pdf_to_txt(source_file)
            if not txt_path:
                return False, None, None, f"PDF 텍스트 추출 실패: {err}"
            final_pdf = pdf_out_dir / source_file.name
            shutil.copy2(str(source_file), str(final_pdf))
            final_txt = out_txt_dir / (source_file.stem + ".txt")
            shutil.move(str(txt_path), str(final_txt))
        else:
            final_txt = out_txt_dir / source_file.name
            shutil.copy2(str(source_file), str(final_txt))
        queue_add("tab2_ready", [_nfc(final_txt.stem)])
        return True, final_txt, final_pdf, f"{final_txt.name} → 장분할 대기 등록"
    except Exception as e:
        return False, None, None, str(e)[:200]
