"""번역: 영어→한국어 고정 — 언어 감지, 단락 분할, 번역 호출, skip/drop 필터."""

import json
import re as _re
from difflib import SequenceMatcher
from pathlib import Path

import llm_providers as llm

from services.common import _save_json_atomic, append_log

_KO_SCRIPT = _re.compile(r"[가-힣]")


def target_lang() -> str:
    return "en"


def needs_translation(txt_path: Path, threshold: float = 0.3) -> bool:
    """한글 비율이 낮으면 번역 필요로 판단."""
    sample = txt_path.read_text(encoding="utf-8", errors="ignore")[:3000]
    ko_ratio = len(_KO_SCRIPT.findall(sample)) / max(len(sample), 1)
    return ko_ratio < threshold


def is_english(txt_path: Path, threshold: float = 0.3) -> bool:
    return needs_translation(txt_path, threshold)


def _ko_ratio(text: str) -> float:
    return len(_KO_SCRIPT.findall(text or "")) / max(len(text or ""), 1)


def _translation_is_valid(src: str, out: str | None) -> bool:
    """번역 결과가 실제 한국어 번역인지 확인한다."""
    if not out:
        return False
    cleaned_src = _re.sub(r"\s+", " ", src or "").strip()
    cleaned_out = _re.sub(r"\s+", " ", out or "").strip()
    if not cleaned_out:
        return False
    if _ko_ratio(cleaned_out) < 0.08:
        return False
    if cleaned_src and SequenceMatcher(None, cleaned_src[:2000], cleaned_out[:2000]).ratio() > 0.82:
        return False
    return True


_HEADING_LIKE_RE = _re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+)\s+.+", _re.I)


def _translate_retry_prompt(paragraph: str) -> str:
    return (
        "Translate the following academic paragraph into Korean. "
        "Preserve numbering such as section numbers or chapter numbers. "
        "Do not leave any sentence or title in English. "
        "If this is a section heading, translate only the heading text while keeping the numbering. "
        "Output ONLY the Korean text.\n\n"
        f"{paragraph}"
    )


def _translate_paragraph(paragraph: str, engine: str, glossary: dict | None = None) -> str | None:
    ko = translate(paragraph, engine, glossary=glossary)
    if _translation_is_valid(paragraph, ko):
        return ko
    if not paragraph.strip():
        return ko
    retry = translate(_translate_retry_prompt(paragraph), engine, glossary=glossary)
    if _translation_is_valid(paragraph, retry):
        return retry
    if _HEADING_LIKE_RE.match(paragraph.strip()):
        heading_retry = translate(
            "This is a section heading from an academic chapter. Translate it into Korean and keep the numbering.\n\n"
            f"{paragraph}",
            engine,
            glossary=glossary,
        )
        if _translation_is_valid(paragraph, heading_retry):
            return heading_retry
    return None


def build_translate_system() -> str:
    """한국어 번역 시스템 프롬프트."""
    return (
        "You are a professional theological/academic translator. "
        "Detect the source language automatically and translate the user's text into Korean. "
        "Proper nouns (personal names, place names): on FIRST mention write the Korean "
        "rendering followed by the original in parentheses; "
        "if a name is listed below as already introduced, write the Korean form ONLY. "
        "Preserve technical terms and scripture references as-is. "
        "Use ONLY plain declarative academic Korean (평서체/하다체): "
        "endings such as -다, -이다, -한다, -였다, -이었다. "
        "DO NOT use any polite/honorific forms — never use -습니다, -입니다, "
        "-해요, -이에요, -지요, -군요, -네요, or any other -요/-니다 endings. "
        "The text may be an incomplete fragment cut mid-sentence (PDF page breaks): "
        "translate it as-is anyway — NEVER comment on it, NEVER ask for more context, "
        "NEVER say the text is incomplete. "
        "Output ONLY the Korean translation, nothing else."
    )

# 번역 엔진 ID (UI 라디오와 1:1)
# 번역 엔진 id = "provider:model". 공급자는 llm_providers.PROVIDERS + Claude CLI(구독).
_translate_error_logged = False


def translate_engine_options() -> list[tuple[str, str, bool, str]]:
    """[(engine_id, label, available, hint)]. 키 있는 공급자만 available=True."""
    opts: list[tuple[str, str, bool, str]] = []
    for prov in llm.API_PROVIDERS:
        info = llm.PROVIDERS[prov]
        avail = llm.has_key(prov)
        for m in info["models"]:
            opts.append((f"{prov}:{m}", f"{m} · {info['label']}", avail, info["hint"]))
    for prov in llm.CLI_PROVIDERS:
        info = llm.PROVIDERS[prov]
        avail = llm.has_key(prov)
        for m in info["models"]:
            opts.append((f"{prov}:{m}", f"{m} · {info['label']}", avail, info["hint"]))
    return opts


def engine_label(engine_id) -> str:
    if not engine_id:
        return "?"
    for eid, lbl, _av, _h in translate_engine_options():
        if eid == engine_id:
            return lbl
    return engine_id


def _merge_dangling(paras: list[str], max_chunk: int = 3000) -> list[str]:
    """PDF 페이지 경계·각주 번호 때문에 문장 중간에서 끊긴 단락을 병합. (2026-06-11)
    이전 단락이 종결부호 없이 끝났거나 현재 단락이 소문자로 시작하면 같은 문장으로 본다."""
    _terminal = _re.compile(r'[.!?:;"”’)\]]\s*$')
    merged: list[str] = []
    for p in paras:
        if merged:
            prev = merged[-1]
            if (not prev.lstrip().startswith("#")          # 제목은 단독 유지
                    and len(prev) + len(p) + 1 <= max_chunk
                    and (not _terminal.search(prev) or _re.match(r"^[a-z]", p))):
                merged[-1] = prev.rstrip() + " " + p.lstrip()
                continue
        merged.append(p)
    return merged


def _split_paragraphs_robust(text_raw: str, target_chunk: int = 1500, min_para: int = 5) -> list[str]:
    """단락 분할 보강. \\n\\n 의존이 실패하면 단일 줄바꿈·문장 단위 fallback.
    OCR 출력 형식에 무관하게 작동. (2026-05-16 신설)

    1차: \\n\\n 분리. 단락 수 ≥ min_para 이고 평균 길이 ≤ target_chunk*2 이면 통과.
    2차: \\n 단일 분리 후 target_chunk 자 단위 누적 청크.
    3차: 문장(. ! ?) 단위 분리 후 target_chunk 자 단위 누적 청크.
    """
    primary = [p.strip() for p in text_raw.split("\n\n") if len(p.strip()) > 50]
    if len(primary) >= min_para:
        avg = sum(len(p) for p in primary) / len(primary)
        if avg <= target_chunk * 2:
            return _merge_dangling(primary)

    # 2차 — 단일 줄바꿈 후 누적 청크
    lines = [ln.strip() for ln in text_raw.split("\n") if ln.strip()]
    chunks: list[str] = []
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) + 1 <= target_chunk:
            buf = (buf + " " + ln).strip() if buf else ln
        else:
            if len(buf) > 50:
                chunks.append(buf)
            buf = ln
    if buf and len(buf) > 50:
        chunks.append(buf)
    if len(chunks) >= min_para:
        return chunks

    # 3차 — 문장 단위 누적 청크
    sentences = _re.split(r"(?<=[.!?])\s+", text_raw.replace("\n", " "))
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    chunks = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) + 1 <= target_chunk:
            buf = (buf + " " + s).strip() if buf else s
        else:
            if len(buf) > 50:
                chunks.append(buf)
            buf = s
    if buf and len(buf) > 50:
        chunks.append(buf)
    return chunks if chunks else primary  # 정말 아무것도 안 잡히면 1차 반환


def translate(text: str, engine: str, glossary: dict | None = None) -> str | None:
    """단락 하나를 'provider:model' 엔진으로 영→한 번역. 실패 시 None(영어 유지).
    glossary: 앞 단락들에서 이미 소개된 고유명사 {원어: 한글} — 한글만 쓰게 지시."""
    global _translate_error_logged
    if not engine or ":" not in engine:
        return None
    provider, model = engine.split(":", 1)
    sys_prompt = build_translate_system()
    if glossary:
        # 이미 소개된 고유명사 — 목표 언어 표기만 쓰게 지시 (최근 80개 제한)
        _pairs = "; ".join(f"{en} = {ko}" for en, ko in list(glossary.items())[-80:])
        sys_prompt += " Already-introduced proper nouns (target-language form only, no parentheses): " + _pairs
    try:
        out = llm.complete(provider, model, sys_prompt, text, max_tokens=8192)
        return out.strip() or None
    except Exception as e:
        if not _translate_error_logged:
            append_log(f"ERROR: 번역 실패 [{engine}] ({type(e).__name__}): {str(e)[:300]}")
            _translate_error_logged = True
        return None


# ─────────────────────────────────────────────────────────────────────────────
# P6. 각주·미주·인용 번역 skip (2026-05-17 추가)
# 학술 인용은 번역 가치 낮음 (저자명·연도·DOI·URL 형식). 원어 보존이 학술 추적
# 에 유리. 본 PDF 검증: 단락의 ~49% skip → 번역 비용·시간 절반 절감.
# ─────────────────────────────────────────────────────────────────────────────

_FOOTNOTE_DAGGER    = _re.compile(r"^\s*†\s")
_CITATION_NUMBERED  = _re.compile(r"^\s*\[?[0-9]+\*?\]?\s+[A-Z][^.]*,\s+[A-Z]")
_CITATION_BULLET    = _re.compile(r"^\s*-\s+[0-9]+\*?\s+[A-Z]")
_CITATION_URL_HEAVY = _re.compile(r"(https?://|arXiv|doi\.org|dx\.doi)", _re.IGNORECASE)
# 단독 페이지번호·그래프 레이블: 숫자·공백·쉼표·점·하이픈만으로 이루어진 짧은 단락
# "100", "80", "3,000 4,000 5,000", "1-10" 등 → 번역 불필요
_PAGE_NUMBER_ONLY   = _re.compile(r"^[\d\s,.\-–—%]+$")
# OCR 분리 또는 일반 각주 번호로 시작하는 단락 감지
# "1 ", "[1] ", "1.", "1)", "1 0 " (OCR split 10), "1 2 " (OCR split 12) 등
_FOOTNOTE_NUM_START = _re.compile(
    r"^\s*(?:"
    r"\[?\d{1,3}\]?[\s.,):]"    # 일반: [1] · 1. · 1) · 1:
    r"|"
    r"\d\s\d[\s.,):]"           # OCR 분리 두 자리: "1 0 " "2 3." 등
    r")\s*\S"
)
# 소제목·목차 오탐 방지: 인용 마커(숫자·참조 키워드) 없는 짧은 텍스트를 각주로 처리 안 함
_RE_CITE_MARKER = _re.compile(
    r"\d|같은|참조|ibid|op\.|p\.|각주|위의|앞의|출처|see\s|cf\.", _re.IGNORECASE
)
_RE_EDITION_INFO = _re.compile(r"^판\s*\d")   # "판 1 쇄…" 등 출판 판수 정보
# 명시적 인용 마커: 쪽수·연도·저자이니셜·성경책·URL 등 — 소제목과 구별
_RE_EXPLICIT_CITE = _re.compile(
    r"같은\s*책|위의\s*책|앞의\s*책|ibid|op\.\s*cit|"
    r"p\.\s*\d+|pp\.\s*\d+|각주\s*\d|"
    r"\d+\s*쪽|쪽[,. ]|"
    r"[A-Z][a-z]{1,15},\s+[A-Z]|"          # Author, I. 패턴
    r"\b(19|20)\d{2}[),]|"                 # (2020) 또는 2020) 연도
    r"마태|누가복음|요한복음|로마서|고린도|갈라디|에베|"
    r"시편\s*\d|잠언\s*\d|창세기|출애굽|이사야|예레미야|"
    r"https?://|doi:\s*10|www\.",
    _re.IGNORECASE
)


def _is_short_heading(text: str) -> bool:
    """목차·소제목(각주 아님) 판별: 20자 이하이고 인용 마커가 없으면 True."""
    text = text.strip()
    if _RE_EDITION_INFO.match(text):   # "판 N 쇄" 형태 = 출판 정보
        return True
    if len(text) > 20:
        return False
    return not _RE_CITE_MARKER.search(text)


def _parse_footnote_number(p: str) -> int | None:
    """단락 선두 각주 번호를 정수로 반환. OCR 분리 숫자("1 0"→10) 포함. 없으면 None.

    오탐 방지:
    - 줄바꿈 포함 → 섹션 제목+본문 합체, None
    - "1.3.4" 형태 소단원 번호 → None
    - 20자 이하 + 인용 마커 없음 → 목차·소제목, None
    """
    p = p.strip()
    # 줄바꿈 포함 = 섹션 본문(제목+내용) → 각주 아님
    if "\n" in p:
        return None
    # OCR 분리 두 자리 숫자 우선 ("1 0 text" → 10)
    m = _re.match(r"^(\d)\s(\d)[\s.,):]\s*\S", p)
    if m:
        remaining = p[m.end() - 1:].strip()
        if _is_short_heading(remaining):
            return None
        return int(m.group(1) + m.group(2))
    # 일반 숫자 (최대 3자리): 구분자가 "."이고 바로 뒤가 숫자면 소수점 → 제외
    m = _re.match(r"^\[?(\d{1,3})\]?([\s.,):])(.)", p)
    if m:
        sep, nxt = m.group(2), m.group(3)
        if sep == "." and nxt.isdigit():   # "1.3.4" 같은 소단원 번호
            return None
        remaining = p[m.end() - 1:].strip()
        if _is_short_heading(remaining):
            return None
        return int(m.group(1))
    return None


def find_sequential_footnotes(paragraphs: list[str], min_run: int = 3,
                               max_len: int = 300) -> set[int]:
    """연속 번호(1,2,3…)로 이루어진 각주 단락 인덱스를 반환.

    조건:
    - 단락이 각주 번호로 시작하고 max_len 이하
    - 3개 이상 연속 증가 번호 묶음(run)이 존재
    OCR 분리 숫자("1 0" = 10)도 처리.

    오탐 방지 (Q&A 문답/목차 구조):
    - 첫 번째 런 위치가 문서 앞 50% 이내 AND 감지 비율 > 15% → 본문 구조로 판정, 빈 셋 반환
    """
    total = len(paragraphs)
    # (index, number) 후보 수집
    candidates: list[tuple[int, int]] = []
    for i, p in enumerate(paragraphs):
        if len(p.strip()) > max_len:
            continue
        n = _parse_footnote_number(p)
        if n is not None and 1 <= n <= 999:
            candidates.append((i, n))

    if len(candidates) < min_run:
        return set()

    skip: set[int] = set()
    # 연속 run 탐지: n, n+1, n+2 … 가 연달아 나오는 구간 찾기
    run_start = 0
    first_run_idx: int | None = None
    for k in range(1, len(candidates)):
        prev_n = candidates[k - 1][1]
        curr_n = candidates[k][1]
        if curr_n != prev_n + 1:
            run_len = k - run_start
            if run_len >= min_run:
                if first_run_idx is None:
                    first_run_idx = candidates[run_start][0]
                for j in range(run_start, k):
                    skip.add(candidates[j][0])
            run_start = k
    # 마지막 run 처리
    run_len = len(candidates) - run_start
    if run_len >= min_run:
        if first_run_idx is None:
            first_run_idx = candidates[run_start][0]
        for j in range(run_start, len(candidates)):
            skip.add(candidates[j][0])

    if not skip:
        return set()

    # Q&A 문답·목차 오탐 방지: 첫 런이 앞 50%에 있고 감지 비율이 15% 초과면 제외
    if first_run_idx is not None and total > 0:
        position_ratio = first_run_idx / total
        detect_ratio   = len(skip) / total
        if position_ratio < 0.5 and detect_ratio > 0.15:
            return set()

    # 명시적 인용 마커 부재 시 오탐 처리: 소제목·통계표 등 비인용 구조
    # 정상 각주는 반드시 쪽수·저자·성경책명·URL 등 하나 이상 포함
    has_any_cite = any(
        _RE_EXPLICIT_CITE.search(paragraphs[i])
        for i in skip
        if i < total
    )
    if not has_any_cite:
        return set()

    return skip

_SKIP_SECTION_NAMES = {
    "references", "bibliography", "works cited", "참고문헌",
    "literaturverzeichnis", "bibliographie", "références",
    "referencias", "参考文献", "referências", "referenties",
    "список литературы", "список источников",   # Russian
    "المراجع", "قائمة المراجع",                  # Arabic
    "ביבליוגרפיה", "מקורות",                      # Hebrew
    "ማጣቀሻዎች",                                    # Amharic
    "tài liệu tham khảo",                        # Vietnamese
    "daftar pustaka", "referensi",               # Indonesian
    "รายการอ้างอิง",                               # Thai
}


def _paragraph_already_target(paragraph: str, threshold: float = 0.6) -> bool:
    """단락에 한글 비율이 threshold 이상이면 이미 번역된 것으로 간주."""
    p = paragraph.strip()
    if not p:
        return False
    hits = len(_KO_SCRIPT.findall(p))
    return (hits / max(len(p), 1)) >= threshold


def should_skip_translation(paragraph: str) -> bool:
    """단락 번역 생략 조건: 인용·각주 (이미 목표 언어 단락은 캐시로 별도 처리)."""
    p = paragraph.strip()
    if not p:
        return True
    if _FOOTNOTE_DAGGER.match(p):
        return True
    if _CITATION_NUMBERED.match(p):
        return True
    if _CITATION_BULLET.match(p):
        return True
    # OCR 분리 포함 각주 번호 시작 + 짧은 단락
    if len(p) < 500 and _FOOTNOTE_NUM_START.match(p):
        return True
    # 짧고 URL 들어간 단락 = 인용일 가능성 (500자 이하 + arXiv/DOI/URL)
    if len(p) < 500 and _CITATION_URL_HEAVY.search(p):
        return True
    return False


def should_drop_paragraph(paragraph: str) -> bool:
    """bilingual에서 완전 제외할 단락 — 번역·미주 어디에도 포함하지 않음.
    페이지 번호, 그래프 Y축 레이블 등 번역 결과물에 불필요한 OCR 잡음."""
    p = paragraph.strip()
    if not p:
        return True
    # 숫자·공백·구두점만으로 이루어진 80자 이하 단락 (페이지번호·그래프레이블)
    if len(p) <= 80 and _PAGE_NUMBER_ONLY.match(p):
        return True
    return False


def find_skip_section_paragraphs(paragraphs: list[str]) -> set[int]:
    """`## References` 헤더 ~ 다음 `## ` 헤더 전까지 단락 인덱스 집합 반환.

    `## Glossary`는 *번역 유지* — 학술 용어 한글 번역이 본 논문 자료로 유용.

    헤더가 없는 미주 영역도 tail 휴리스틱으로 자동 감지 (2026-05-18 추가):
    PDF→MD 변환 과정에서 References/Bibliography 헤더가 누락된 경우, 단락 끝쪽의
    마지막 *narrative* 단락(>=400자, 인용 신호 없음) 이후가 미주로 추정되면 skip.
    """
    skip_idxs: set[int] = set()
    in_skip = False
    for i, p in enumerate(paragraphs):
        stripped = p.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            if section in _SKIP_SECTION_NAMES:
                in_skip = True
                skip_idxs.add(i)
                continue
            in_skip = False
            continue
        if in_skip:
            skip_idxs.add(i)

    # tail 자동 감지: 헤더 기반 skip이 *없을 때만* 발동 (오탐 방지)
    if not skip_idxs and len(paragraphs) >= 50:
        scan_start = int(len(paragraphs) * 0.6)
        last_narrative = -1
        for i in range(len(paragraphs) - 1, scan_start - 1, -1):
            p = paragraphs[i].strip()
            if (
                len(p) >= 400
                and not _CITATION_URL_HEAVY.search(p)
                and not _CITATION_NUMBERED.match(p)
                and not _CITATION_BULLET.match(p)
                and not _FOOTNOTE_DAGGER.match(p)
                and not _FOOTNOTE_NUM_START.match(p)
            ):
                last_narrative = i
                break
        if 0 <= last_narrative < len(paragraphs) - 5:
            for i in range(last_narrative + 1, len(paragraphs)):
                skip_idxs.add(i)

    return skip_idxs


_HANGUL_RE = _re.compile(r'[가-힣ᄀ-ᇿ㄰-㆏]')

def _needs_translation(stem: str) -> bool:
    """책 제목(stem)에 한글이 없으면 번역 필요(영문 등), 한글 있으면 번역 불필요."""
    return not bool(_HANGUL_RE.search(stem))


def translate_one_chapter(ch_path: Path, engine: str, progress_cb=None) -> tuple[bool, str]:
    """단일 챕터 TXT 번역 → _ko.txt 저장. (ok, msg).

    중단 대비 (2026-07-03): Streamlit rerun 등으로 도중에 죽어도 진행분이
    읽을 수 있는 _ko.partial.md로 남는다. 완주하면 _ko.txt로 확정하고
    partial·progress 캐시를 정리한다.
    (.md인 이유: .txt면 챕터 목록 glob(??_*.txt)에 원문으로 오인된다.)"""
    try:
        text = ch_path.read_text(encoding="utf-8", errors="ignore")
        ko_path = ch_path.with_name(ch_path.stem + "_ko.txt")
        partial_path = ch_path.with_name(ch_path.stem + "_ko.partial.md")
        progress_path = ch_path.with_name(ch_path.stem + "_ko.progress.json")
        if not needs_translation(ch_path):
            ko_path.write_text(text, encoding="utf-8")
            partial_path.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)
            return True, "이미 한국어 — 그대로 복사"
        paras = _split_paragraphs_robust(text)
        out: list[str] = []
        translated_n = preserved_n = dropped_n = failed_n = resumed_n = api_calls = 0
        total = len(paras) or 1

        def _save_partial():
            tmp = partial_path.with_name(partial_path.name + ".tmp")
            tmp.write_text("\n\n".join(out), encoding="utf-8")
            tmp.replace(partial_path)
        cached_rows: dict[int, dict] = {}
        if progress_path.exists():
            try:
                loaded = json.loads(progress_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    cached_rows = {
                        int(row.get("idx")): row
                        for row in loaded
                        if isinstance(row, dict) and isinstance(row.get("idx"), int)
                    }
            except Exception:
                cached_rows = {}
        for idx, p in enumerate(paras, 1):
            cached = cached_rows.get(idx)
            if cached and cached.get("src") == p and isinstance(cached.get("tgt"), str):
                status = cached.get("status")
                tgt = cached.get("tgt", "")
                if status == "dropped":
                    dropped_n += 1
                else:
                    out.append(tgt)
                    if status == "preserved":
                        preserved_n += 1
                    elif status == "failed":
                        failed_n += 1
                    else:
                        translated_n += 1
                resumed_n += 1
                if progress_cb:
                    progress_cb(idx, total, translated_n, preserved_n, dropped_n, failed_n, resumed_n, api_calls)
                continue
            if should_drop_paragraph(p):
                dropped_n += 1
                cached_rows[idx] = {"idx": idx, "src": p, "tgt": "", "status": "dropped"}
                _save_json_atomic(progress_path, [cached_rows[i] for i in sorted(cached_rows)])
                _save_partial()
                if progress_cb:
                    progress_cb(idx, total, translated_n, preserved_n, dropped_n, failed_n, resumed_n, api_calls)
                continue
            if should_skip_translation(p):
                out.append(p)
                preserved_n += 1
                cached_rows[idx] = {"idx": idx, "src": p, "tgt": p, "status": "preserved"}
            else:
                ko = _translate_paragraph(p, engine)
                api_calls += 1
                if _translation_is_valid(p, ko):
                    out.append(ko)
                    translated_n += 1
                    cached_rows[idx] = {"idx": idx, "src": p, "tgt": ko, "status": "translated"}
                else:
                    out.append(p)
                    failed_n += 1
                    cached_rows[idx] = {"idx": idx, "src": p, "tgt": p, "status": "failed"}
            _save_json_atomic(progress_path, [cached_rows[i] for i in sorted(cached_rows)])
            _save_partial()
            if progress_cb:
                progress_cb(idx, total, translated_n, preserved_n, dropped_n, failed_n, resumed_n, api_calls)
        detail = f"{len(out)}단락 처리 완료 · 재사용 {resumed_n} · 신규번역 {translated_n} · 원문보존 {preserved_n}"
        if dropped_n:
            detail += f" · 삭제 {dropped_n}"
        if failed_n:
            detail += f" · 실패보존 {failed_n}"
        if translated_n == 0:
            ko_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
            return False, detail + " — 유효한 한국어 번역 결과가 없습니다"
        ko_path.write_text("\n\n".join(out), encoding="utf-8")
        # 완주 — 중간 산출물 정리 (partial은 _ko.txt로 확정됨, progress 캐시 소진)
        partial_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        return True, detail
    except Exception as e:
        return False, str(e)[:200]
