"""PDF 장(chapter) 지도 판독 — 장분할 Tier 0 (2026-07-07).

텍스트 층은 OCR로 열화되지만(오탈자·헤딩 소실) PDF의 메타데이터와 시각
구조는 살아 있다. 두 경로로 장 지도를 얻는다:

  0a. 북마크(outline) 메타데이터 — 출판 PDF. 무료·결정적.
  0b. 시각 판독 — 책 앞부분(차례 페이지 포함)을 **연결된 LLM 공급자**
      (Gemini/OpenAI/Anthropic API 또는 Claude CLI)에 보내 차례를 읽게 한다.
      특정 모델에 고정하지 않고 wiki_provider_model() 설정을 따른다.

어느 경로든 결과는 "장 제목 목록(+쪽수 힌트)"이고, 실제 분할 위치는
TXT에서 제목을 퍼지 탐색해 로컬에서 확정한다(환각 무해화·OCR 오탈자 흡수).
"""

import base64
import json
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

import llm_providers as llm

from services.common import append_log

MAX_CHAPTERS = 40
_SCAN_PAGES = 40          # 시각 판독에 보낼 앞부분 페이지 수 (차례는 보통 앞 20쪽 내)
_MIN_GAP = 1500           # 장 최소 간격(문자)


# ── 0a. 북마크(outline) ────────────────────────────────────

def pdf_bookmarks(pdf_path: Path) -> list[tuple[str, int]] | None:
    """PDF 목차 메타데이터 → [(제목, 0-기반 페이지)]. 최상위 레벨만."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            toc = []
            for b in pdf.get_toc():
                title = (getattr(b, "title", "") or "").strip()
                pi = getattr(b, "page_index", None)
                if pi is None:                     # pypdfium2 버전별 API 차이 흡수
                    dest = getattr(b, "dest", None)
                    pi = getattr(dest, "page_index", None) if dest else None
                if title and pi is not None:
                    toc.append((title, pi, getattr(b, "level", 0)))
        finally:
            pdf.close()
    except Exception as e:
        append_log(f"WARN: PDF 북마크 읽기 실패 ({type(e).__name__}) {str(e)[:120]}")
        return None
    if not toc:
        return None
    top = min(lv for _t, _p, lv in toc)
    rows = [(t.strip(), p) for t, p, lv in toc if lv == top and t.strip()]
    return rows if len(rows) >= 3 else None


# ── 0b. 시각 판독 (공급자 라우팅) ─────────────────────────────

_VISUAL_TOC_PROMPT = """이 PDF는 한 권의 책에서 추려낸 페이지들입니다 (차례가 있을 만한 앞부분 + 장 구분처럼 보이는 중간 페이지들).
이 PDF의 k번째 페이지가 원본 책 PDF의 몇 페이지인지는 아래 매핑을 참고하세요.

[원본 페이지 매핑 (1-기반)]
{mapping}

할 일: 책의 **장(chapter) 목록**을 읽기 순서대로 파악하세요.
- 차례(목차) 페이지가 있으면 그것을 판독
- 차례가 없으면 장 구분 페이지(장 번호·제목만 크게 인쇄된 페이지)나 본문 장 시작으로 판단
- **층위 선택**: 책을 순서대로 나눠 읽기에 적절한 단위를 고른다.
  최상위 구분이 부(部)/권(Part)처럼 2~3개뿐이면 그 **한 단계 아래**
  (장/이야기/강 등)를 장으로 삼고, 그 아래 소제목·절·항은 제외
- 제목은 인쇄된 그대로
- page에는 **그 장의 본문이 시작되는 원본 페이지 번호**(위 매핑 기준 1-기반)를,
  이 PDF에서 확인 불가하면 null
- 머리말/판권/부록/참고문헌은 장이 아니면 제외

출력은 JSON 하나만: {{"chapters": [{{"title": "...", "page": 12}}]}}"""


def _page_char_counts(txt: str) -> list[int]:
    """\\f 기준 페이지별 실제 글자 수(공백 제외)."""
    return [len(re.sub(r"\s+", "", p)) for p in txt.split("\f")]


def _scan_page_indices(txt: str, total_pages: int) -> list[int]:
    """시각 판독에 보낼 페이지(0-기반) 선정 — 앞부분(차례 후보) + 희박 페이지(장 구분 후보).
    장 구분 페이지는 글자가 거의 없어 텍스트 층에서 로컬로 식별 가능하다."""
    counts = _page_char_counts(txt)
    n = min(total_pages, len(counts))
    picked = set(range(min(18, n)))                     # 차례는 보통 앞 18쪽 내
    body = [c for c in counts[:n] if c > 0]
    med = sorted(body)[len(body) // 2] if body else 0
    thresh = max(60, int(med * 0.18))
    sparse = [i for i in range(2, n) if counts[i] <= thresh]
    for i in sparse[:30]:                               # 구분 페이지 + 다음 쪽(장 제목 확인용)
        picked.add(i)
        if i + 1 < n:
            picked.add(i + 1)
    out = sorted(picked)
    return out[:55]                                     # 공급자 페이지 한도·비용 상한


def _build_scan_pdf(pdf_path: Path, page_indices: list[int]) -> Path | None:
    """지정 페이지만 추린 임시 PDF 생성."""
    try:
        import pypdfium2 as pdfium
        src = pdfium.PdfDocument(str(pdf_path))
        try:
            total = len(src)
            pages = [i for i in page_indices if i < total]
            if not pages:
                return None
            out = pdfium.PdfDocument.new()
            out.import_pages(src, pages=pages)
            dst = Path(tempfile.gettempdir()) / f"toc_scan_{pdf_path.stem[:30]}.pdf"
            out.save(str(dst))
            out.close()
            return dst
        finally:
            src.close()
    except Exception as e:
        append_log(f"WARN: 스캔용 PDF 추출 실패 ({type(e).__name__}) {str(e)[:120]}")
        return None


def _visual_toc_gemini(model: str, key: str, scan_pdf: Path, prompt: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=scan_pdf.read_bytes(),
                                        mime_type="application/pdf"),
                  prompt],
        config={"temperature": 0.1, "response_mime_type": "application/json",
                "max_output_tokens": 4096})
    return resp.text or ""


def _visual_toc_anthropic(model: str, key: str, scan_pdf: Path, prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model, max_tokens=4096, temperature=0.1,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(scan_pdf.read_bytes()).decode()}},
            {"type": "text", "text": prompt +
             "\n\n반드시 유효한 JSON 객체 하나만 출력하라."},
        ]}])
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _visual_toc_openai(model: str, key: str, scan_pdf: Path, prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=key)
    up = client.files.create(file=open(scan_pdf, "rb"), purpose="user_data")
    try:
        resp = client.responses.create(
            model=model, temperature=0.1,
            input=[{"role": "user", "content": [
                {"type": "input_file", "file_id": up.id},
                {"type": "input_text", "text": prompt +
                 "\n\n반드시 유효한 JSON 객체 하나만 출력하라."},
            ]}])
        return resp.output_text or ""
    finally:
        try:
            client.files.delete(up.id)
        except Exception:
            pass


def _render_pages_png(pdf_path: Path, page_indices: list[int],
                      scale: float = 1.3, max_pages: int = 40) -> list[Path]:
    """지정 페이지를 이미지로 렌더링 — PDF를 직접 못 받는 공급자(codex_cli)용.
    차례/장제목 판독엔 저해상 JPEG면 충분 — 전송량 절약."""
    import pypdfium2 as pdfium
    out: list[Path] = []
    tmp = Path(tempfile.gettempdir()) / f"toc_png_{pdf_path.stem[:24]}"
    tmp.mkdir(exist_ok=True)
    src = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in page_indices[:max_pages]:
            if i >= len(src):
                continue
            bmp = src[i].render(scale=scale)
            f = tmp / f"p{i + 1:04d}.jpg"
            bmp.to_pil().convert("RGB").save(str(f), quality=72)
            out.append(f)
    finally:
        src.close()
    return out


def _visual_toc_codex_cli(model: str, pngs: list[Path], prompt: str) -> str:
    """Codex CLI(구독) — PDF 입력이 없어 페이지 PNG를 -i로 첨부해 판독."""
    cli = llm.codex_cli_path()
    if not cli:
        raise RuntimeError("codex CLI 없음")
    out_file = Path(tempfile.gettempdir()) / f"codex_toc_{Path(pngs[0]).parent.name}.txt"
    args = [cli, "exec", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "-o", str(out_file)]
    for f in pngs:
        args += ["-i", str(f)]
    if model not in ("default", ""):
        args += ["-m", model]
    args.append("-")
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=600,
            cwd=tempfile.gettempdir(), encoding="utf-8", errors="replace",
            input=prompt + "\n\n분석 과정 설명 없이 반드시 유효한 JSON 객체 하나만 출력하라.",
            **llm._no_window_kwargs(),
        )
        if r.returncode != 0:
            raise RuntimeError(f"codex CLI exit {r.returncode}: {(r.stderr or '')[:300]}")
        if out_file.exists():
            return out_file.read_text(encoding="utf-8").strip()
        return (r.stdout or "").strip()
    finally:
        out_file.unlink(missing_ok=True)


def _visual_toc_claude_cli(model: str, scan_pdf: Path, prompt: str) -> str:
    """Claude CLI(구독)로 PDF 시각 판독 — Read 도구만 허용해 파일을 읽게 한다."""
    cli = llm.claude_cli_path()
    if not cli:
        raise RuntimeError("claude CLI 없음")
    prompt = (f'PDF 파일 "{scan_pdf}" 을 Read 도구로 읽어라 (필요하면 pages를 나눠 여러 번). '
              + prompt
              + "\n\n분석 과정 설명 없이 반드시 유효한 JSON 객체 하나만 출력하라.")
    r = subprocess.run(
        [cli, "-p", prompt, "--model", model, "--output-format", "text",
         "--allowedTools", "Read",
         "--system-prompt", "Output only one valid JSON object."],
        capture_output=True, text=True, timeout=600, cwd=str(scan_pdf.parent),
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        **llm._no_window_kwargs(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI exit {r.returncode}: {(r.stderr or '')[:200]}")
    return (r.stdout or "").strip()


def pdf_visual_toc(pdf_path: Path, txt: str | None = None) -> list[tuple[str, int | None]] | None:
    """연결된 공급자로 차례/장구분 페이지 시각 판독 → [(제목, 원본페이지 1-기반|None)].
    실패·미지원 공급자·후보 부족 시 None."""
    try:
        prov, model = llm.wiki_provider_model()
        if not llm.has_key(prov):
            return None
    except Exception:
        return None
    if txt is None:
        return None
    try:
        import pypdfium2 as pdfium
        _src = pdfium.PdfDocument(str(pdf_path))
        total_pages = len(_src)
        _src.close()
    except Exception:
        return None
    idxs = _scan_page_indices(txt, total_pages)
    if prov == "codex_cli":
        idxs = idxs[:40]                           # 이미지 첨부 상한
    if len(idxs) < 3:
        return None
    mapping = ", ".join(f"{k + 1}번째→원본 {i + 1}p" for k, i in enumerate(idxs))
    prompt = _VISUAL_TOC_PROMPT.format(mapping=mapping)
    scan: Path | None = None
    pngs: list[Path] = []
    try:
        if prov == "codex_cli":
            # PDF 직접 입력이 없는 CLI — 페이지를 PNG로 렌더링해 -i로 첨부
            pngs = _render_pages_png(pdf_path, idxs)
            if len(pngs) < 3:
                return None
            prompt_img = prompt.replace(
                "이 PDF는 한 권의 책에서 추려낸 페이지들입니다",
                "첨부된 이미지들은 한 권의 책에서 추려낸 페이지들입니다").replace(
                "이 PDF의 k번째 페이지가", "k번째 이미지가")
            raw = _visual_toc_codex_cli(model, pngs, prompt_img)
        else:
            scan = _build_scan_pdf(pdf_path, idxs)
            if scan is None:
                return None
            if prov == "gemini":
                raw = _visual_toc_gemini(model, llm.get_key(prov), scan, prompt)
            elif prov == "anthropic":
                raw = _visual_toc_anthropic(model, llm.get_key(prov), scan, prompt)
            elif prov == "openai":
                raw = _visual_toc_openai(model, llm.get_key(prov), scan, prompt)
            elif prov == "claude_cli":
                raw = _visual_toc_claude_cli(model, scan, prompt)
            else:
                return None
        data = _parse_json_lenient(raw)
    except Exception as e:
        append_log(f"WARN: PDF 시각 차례 판독 실패 [{prov}] ({type(e).__name__}) {str(e)[:200]}")
        return None
    finally:
        try:
            if scan is not None:
                scan.unlink(missing_ok=True)
            for f in pngs:
                f.unlink(missing_ok=True)
        except Exception:
            pass
    rows = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    out: list[tuple[str, int | None]] = []
    for r in rows:
        title = str((r or {}).get("title") or "").strip()
        if not title or len(title) > 90:
            continue
        page = r.get("page")
        out.append((title, int(page) if isinstance(page, (int, float)) else None))
    if not (3 <= len(out) <= MAX_CHAPTERS):
        return None
    append_log(f"PDF 시각 차례 판독 [{prov}]: {len(out)}개 장 — {pdf_path.name}")
    return out


def _parse_json_lenient(raw: str) -> dict:
    """JSON만 출력하라는 지시를 어기고 산문이 섞여도 첫 JSON 객체를 건진다."""
    raw = llm._strip_fence(raw)
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


# ── 제목 → TXT 위치 확정 (퍼지 탐색, 로컬) ───────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE).lower()


def _locate_titles(txt: str, titles: list[tuple[str, int | None]]) -> list[tuple[int, str]]:
    """제목 목록을 본문에서 순차 퍼지 탐색 → [(문자위치, 제목)].
    OCR 오탈자 흡수(유사도), 차례 구간 회피(같은 제목 다중 매칭 시 순차 그리디)."""
    lines: list[tuple[int, str]] = []
    pos = 0
    for raw in txt.splitlines(True):
        s = raw.strip()
        if 2 <= len(s) <= 80:
            lines.append((pos, _norm(s)))
        pos += len(raw)
    total = len(txt)
    found: list[tuple[int, str]] = []
    prev_end = 0
    for k, (title, _page) in enumerate(titles):
        nt = _norm(title)
        if len(nt) < 2:
            continue
        cands: list[tuple[float, int]] = []
        for p, nl in lines:
            if p < prev_end:
                continue
            if not nl:
                continue
            r = SequenceMatcher(None, nt, nl[:len(nt) + 10]).ratio()
            if r >= 0.72:
                cands.append((r, p))
        if not cands:
            continue
        # 첫 장: 매칭이 여럿이면(차례+본문) 문서 앞 6% 안의 것은 차례로 보고 뒤 것 우선
        if k == 0 and len(cands) >= 2:
            body = [(r, p) for r, p in cands if p > total * 0.06]
            if body:
                cands = body
        cands.sort(key=lambda x: (-x[0], x[1]))
        best = cands[0][1]
        found.append((best, title))
        prev_end = best + _MIN_GAP
    found.sort(key=lambda x: x[0])
    dedup: list[tuple[int, str]] = []
    for p, t in found:
        if dedup and p - dedup[-1][0] < _MIN_GAP:
            continue
        dedup.append((p, t))
    return dedup


def _page_offsets(txt: str) -> list[int]:
    """\\f(페이지 구분자) 기준 페이지 시작 문자 위치 목록."""
    offs = [0]
    for m in re.finditer("\f", txt):
        offs.append(m.end())
    return offs


def _split_at(txt: str, marks: list[tuple[int, str]]) -> list[tuple[str, str]] | None:
    if len(marks) < 3:
        return None
    positions = [p for p, _t in marks]
    bounds = positions + [len(txt)]
    chapters: list[tuple[str, str]] = []
    for i, (_p, title) in enumerate(marks):
        body = txt[bounds[i]:bounds[i + 1]].strip()
        if len(body) < 300 and chapters:
            chapters[-1] = (chapters[-1][0], chapters[-1][1] + "\n\n" + body)
            continue
        chapters.append((f"{len(chapters) + 1}. {title}", body))
    return chapters if len(chapters) >= 3 else None


# ── 공개 API: PDF 기반 장 분할 ───────────────────────────────

def pdf_chapter_split(txt: str, pdf_path: Path) -> tuple[str, list[tuple[str, str]]] | None:
    """(mode, chapters) 또는 None. mode=bookmark|visual."""
    if not pdf_path or not Path(pdf_path).exists():
        return None
    pdf_path = Path(pdf_path)

    # 0a. 북마크 — 페이지 인덱스를 \f 매핑으로 직접 위치 변환 (가장 결정적)
    bm = pdf_bookmarks(pdf_path)
    if bm:
        offs = _page_offsets(txt)
        marks = [(offs[p], t) for t, p in bm if p < len(offs)]
        marks = [m for m in marks if m[0] > 0 or bm[0][1] == 0]
        chs = _split_at(txt, sorted(set(marks), key=lambda x: x[0]))
        if chs:
            append_log(f"장분할: PDF 북마크 {len(chs)}챕터 — {pdf_path.name}")
            return "bookmark", chs

    # 0b. 시각 판독 — 페이지 번호는 \f 매핑으로 결정적 변환, 없으면 제목 퍼지 탐색
    titles = pdf_visual_toc(pdf_path, txt)
    if titles:
        offs = _page_offsets(txt)
        marks: list[tuple[int, str]] = []
        fuzzy_needed: list[tuple[str, int | None]] = []
        for title, page in titles:
            if page is not None and 1 <= page <= len(offs):
                marks.append((offs[page - 1], title))
            else:
                fuzzy_needed.append((title, page))
        if fuzzy_needed:
            marks.extend(_locate_titles(txt, fuzzy_needed))
        marks = sorted(set(marks), key=lambda x: x[0])
        dedup: list[tuple[int, str]] = []
        for p, t in marks:
            if dedup and p - dedup[-1][0] < _MIN_GAP:
                continue
            dedup.append((p, t))
        chs = _split_at(txt, dedup)
        if chs:
            missing = len(titles) - len(chs)
            append_log(f"장분할: PDF 시각 판독 {len(chs)}챕터"
                       + (f" (제목 {missing}개 위치 미확정)" if missing > 0 else "")
                       + f" — {pdf_path.name}")
            return "visual", chs
    return None
