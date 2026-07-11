#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""챕터 모드 위키 생성기 (2026-06-09). gemini_wiki.py 보완 모듈.

긴 책을 '진짜 장 구조'가 있을 때만 장별로 생성(제목은 원전 그대로, 작명 금지).
분할 캐스케이드: ①MD ## 헤딩 → ②인쇄된 목차(TOC) 복원 → ③둘 다 없으면 단일.
출력: A(한 노트 허브) + B(장별 개별 노트). 모드 full / add-chapters / (A단독은 추후).
"""
import os, re, sys, json, time, datetime, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path.home() / ".local/bin"))
import config as cfg
import gemini_wiki as gw   # nfc, rebuild_citations, make_filename, OUT_DIR, get_key
import llm_providers as llm
import source_metadata as smeta

DONE_DIR = cfg.DONE_DIR
MAX_CHAPTERS = 30
CHAPTER_MIN_CHARS = 2500

_IMG_RE  = re.compile(r"!\[[^\]]*\]\([^)]*\)\s*")
_HEAD_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_NOTE_TITLE_RE = re.compile(r"^\s*\d+[.)]\s")
_NOTE_SECTION = {"notes", "endnotes", "bibliography", "references", "index",
                 "works cited", "acknowledgments", "주", "미주", "참고문헌", "찾아보기"}
_RUNHEAD = None   # 책별 러닝헤더(책제목) 정규식 — chapter_split에서 설정


# ── MD 찾기 ──
def find_layout_md(stem: str):
    target = gw.nfc(stem)
    for md in DONE_DIR.glob("*/2_md/*.md"):
        if gw.nfc(md.stem) == target:
            return md
    return None

def find_txt(stem: str):
    target = gw.nfc(stem)
    for f in DONE_DIR.glob("*/1_txt/*.txt"):
        if gw.nfc(f.stem) == target:
            return f
    # 폴백: PROCESSED_DIR 평면 구조 (gemini_wiki 동일 소스)
    for f in gw.SRC_DIR.glob("*.txt"):
        if gw.nfc(f.stem) == target:
            return f
    return None

def _strip_noise(md: str) -> str:
    md = _IMG_RE.sub("", md)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)
    return md.replace("\x0c", "\n")

def _is_note_title(t: str) -> bool:
    s = t.strip().lower()
    return bool(_NOTE_TITLE_RE.match(t.strip())) or s in _NOTE_SECTION


# ── 한국어 주요 장 제목 감지 ──
_KO_MAJOR_CH_RE = re.compile(
    r"^(?:제\s*)?(\d+)\s*장\b"           # "1장", "제 2 장" 등
    r"|^(머리말|서론|서문|결론|에필로그|프롤로그|후기|맺음말|맺는말|들어가며|나가며)$",
    re.IGNORECASE,
)

def _is_ko_major_chapter(title: str) -> bool:
    return bool(_KO_MAJOR_CH_RE.match(title.strip()))


# ── ① MD ## 헤딩 기반(의미 단위) ──
def heading_chapters(md: str):
    lines = md.split("\n")

    # 한국어 장 구조가 있으면 N장/머리말 헤딩만 분할 기준으로 사용
    headings = [_HEAD_RE.match(ln) for ln in lines]
    ko_major = [m.group(2).strip() for m in headings if m and _is_ko_major_chapter(m.group(2))]
    use_ko_only = len(ko_major) >= 2   # N장 제목이 2개 이상이면 한국어 모드

    segs, cur = [], {"title": "서두", "lines": []}
    for ln in lines:
        m = _HEAD_RE.match(ln)
        if m and not _is_note_title(m.group(2)):
            title = m.group(2).strip()
            if use_ko_only and not _is_ko_major_chapter(title):
                cur["lines"].append(ln)   # 소절 헤딩은 본문에 포함
                continue
            if cur["lines"] or segs:
                segs.append(cur)
            cur = {"title": title, "lines": [ln]}
        else:
            cur["lines"].append(ln)
    segs.append(cur)
    chs = [(s["title"], "\n".join(s["lines"]).strip())
           for s in segs if "\n".join(s["lines"]).strip()]
    merged = []
    for t, b in chs:
        if merged and len(b) < CHAPTER_MIN_CHARS:
            merged[-1][1] += "\n\n" + b
        else:
            merged.append([t, b])
    total = sum(len(b) for _, b in merged) or 1
    big = max((len(b) for _, b in merged), default=0)
    if len(merged) >= 3 and big < 0.60 * total:          # 신뢰 가능한 의미 단위
        while len(merged) > MAX_CHAPTERS:
            i = min(range(1, len(merged)), key=lambda k: len(merged[k][1]))
            merged[i-1][1] += "\n\n" + merged[i][1]; del merged[i]
        return [(t, b) for t, b in merged]
    return None


# ── ② 인쇄된 목차(TOC) 복원 ──
def _mk_re(text: str):
    toks = re.findall(r"[A-Za-z]+", text)
    words = []
    for tok in toks:
        words += re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[A-Z]", tok) or [tok]
    words = words[:9]
    if len([w for w in words if len(w) >= 3]) < 2:
        return None
    return re.compile(r"[^A-Za-z]*".join(words), re.I)

def parse_toc(txt: str, head_chars: int = 14000):
    lines = txt[:head_chars].split("\n")
    entries, expect = [], 1
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(\d+)\.\s+([A-Za-z].*\S)\s*$", ln)
        if not m:
            continue
        num, title = int(m.group(1)), m.group(2).strip()
        cand = expect if (num == expect or num % 10 == expect) else num
        if cand != expect:
            continue
        sub = []
        for nxt in lines[i+1:i+3]:
            s = nxt.strip()
            if not s or re.match(r"^\s*\d+\.\s", s) or re.fullmatch(r"[\d\s.]+", s):
                break
            sub.append(re.sub(r"\s+\d+\s*$", "", s).strip())
            if len(" ".join(sub)) > 22:
                break
        entries.append((expect, [title] + sub)); expect += 1
    return entries

_ROMAN_VAL = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

def _roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        val = _ROMAN_VAL.get(ch, 0)
        total += -val if val < prev else val
        prev = max(prev, val)
    return total

def _clean_toc_title(title: str) -> tuple[str, bool]:
    raw = re.sub(r"\s+", " ", title).strip()
    cleaned = re.sub(r"\s*[.·ㆍ…]{2,}\s*(?:\d+|[ivxlcdm]+)\s*$", "", raw, flags=re.I).strip()
    cleaned = re.sub(r"\s*(.)\1{3,}\s*(?:\d+|[ivxlcdm]+)\s*$", "", cleaned, flags=re.I).strip()
    cleaned = cleaned.strip(" .·ㆍ…")
    return cleaned, cleaned != raw

def parse_roman_toc(txt: str, head_chars: int = 18000):
    lines = txt[:head_chars].split("\n")
    entries, seen = [], set()
    for ln in lines:
        s = re.sub(r"\s+", " ", ln).strip()
        m = re.match(r"^([IVX]+)\.\s+(.+)$", s)
        if not m:
            continue
        roman = m.group(1).upper()
        title, had_leader = _clean_toc_title(m.group(2))
        if not had_leader or not title or roman in seen:
            continue
        entries.append((roman, title))
        seen.add(roman)
    if len(entries) < 3:
        return []
    nums = [_roman_to_int(r) for r, _ in entries]
    if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
        return []
    return entries

def _candidates(tl):
    order = (tl[1:] + tl[:1]) if len(tl) > 1 else tl
    return [r for r in (_mk_re(t) for t in order) if r]

def _toc_end(txt, toc):
    end = 0
    for _, tl in toc:
        r = _mk_re(tl[0])
        if r:
            m = r.search(txt)
            if m: end = max(end, m.end())
    return end + 50

def _notes_start(body, after):
    win = 4000
    for p in range(after, max(after, len(body) - win), 1500):
        chunk = body[p:p+win]
        if len(re.findall(r"(?m)^\s*\d+\.\s+[A-Z]", chunk)) >= 8:
            m = re.search(r"(?m)^\s*\d+\.\s+[A-Z]", chunk)
            return p + (m.start() if m else 0)
    return len(body)

def _find_roman_heading(txt: str, roman: str, title: str, start: int):
    words = [re.escape(w) for w in re.findall(r"\S+", title)]
    if not words:
        return None
    title_pat = r"\s+".join(words)
    pat = re.compile(rf"(?m)^\s*{re.escape(roman)}\.\s+{title_pat}\s*$", re.I)
    m = pat.search(txt, start)
    if m:
        return m.start()

    # OCR often changes spacing in Korean headings; fall back to same roman marker
    # and a title prefix on a non-TOC line.
    prefix = re.escape(title[: min(len(title), 12)])
    pat = re.compile(rf"(?m)^\s*{re.escape(roman)}\.\s+.*{prefix}.*$", re.I)
    for m in pat.finditer(txt, start):
        line = m.group(0)
        if not re.search(r"(?:[.·ㆍ…]{2,}|(.)\1{3,})\s*(?:\d+|[ivxlcdm]+)\s*$", line, re.I):
            return m.start()
    return None

def roman_toc_split(txt: str):
    toc = parse_roman_toc(txt)
    if len(toc) < 3:
        return None
    cur = _toc_end(txt, [(i + 1, [title]) for i, (_, title) in enumerate(toc)])
    positions, titles = [], []
    for roman, title in toc:
        pos = _find_roman_heading(txt, roman, title, cur)
        if pos is None:
            return None
        positions.append(pos)
        titles.append(f"{roman}. {title}")
        cur = pos + 200
    if positions != sorted(positions):
        return None
    end = _notes_start(txt, positions[-1] + 3000)
    bounds = positions + [end]
    return [(titles[k], txt[bounds[k]:bounds[k+1]].strip()) for k in range(len(positions))]

def toc_split(txt: str):
    roman_chs = roman_toc_split(txt)
    if roman_chs:
        return roman_chs

    toc = parse_toc(txt)
    if len(toc) < 3:
        return None
    cur = _toc_end(txt, toc)
    positions, titles = [], []
    for num, tl in toc:
        cands = _candidates(tl)
        if not cands:
            return None
        hits = [m.start() for m in (r.search(txt, cur) for r in cands) if m]
        if not hits:
            return None
        pos = min(hits); positions.append(pos)
        main = tl[0].strip()
        if main.rstrip().endswith((":", "：")) and len(tl) > 1:
            text = main.rstrip("：: ") + ": " + tl[1].strip()
        else:
            text = main
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        text = re.sub(r"\s+", " ", text).strip(" :：")
        titles.append(f"{num}. {text}")
        cur = pos + 200
    if positions != sorted(positions):
        return None
    end = _notes_start(txt, positions[-1] + 3000)
    bounds = positions + [end]
    return [(titles[k], txt[bounds[k]:bounds[k+1]].strip()) for k in range(len(positions))]


def _clean_heading_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*[.·ㆍ…]{2,}\s*\d+\s*$", "", title).strip()
    title = re.sub(r"\s*[_\-–—|/]\s*\d+\s*$", "", title).strip()
    title = re.sub(r"\s+\d{1,4}\s*$", "", title).strip()
    return title.strip(" .:：;·ㆍ…-_|")


def _title_case_if_english(title: str) -> str:
    if re.search(r"[가-힣]", title):
        return title
    if title.isupper():
        return title.title()
    return title


def _heading_candidates(txt: str):
    """Return sequential major heading candidates from Korean/English books and papers."""
    candidates = []
    generic_numbered_allowed = len(txt) < 180_000
    # (offset, 원본줄) 선구축 — 단독 번호줄+제목줄 결합 감지에 look-ahead 필요 (2026-07-07)
    _lines: list[tuple[int, str]] = []
    line_start = 0
    for raw in txt.splitlines(True):
        _lines.append((line_start, raw.rstrip("\r\n")))
        line_start += len(raw)
    for _li, (pos, line) in enumerate(_lines):
        s = re.sub(r"\s+", " ", line).strip()
        if not s or len(s) > 140:
            continue
        if re.search(r"(?:[.·ㆍ…]{3,}|(.)\1{4,})\s*\d+\s*$", s):
            continue
        num, title = None, ""
        m = re.match(r"^(?:제\s*)?(\d{1,2})\s*장\s*[\.:：)>〉\-–—]?\s*(.*)$", s)
        if m:
            cand_title = m.group(2)
            # 각주·본문 문장 오탐 가드 — "1장 1절 이하 참조." 같은 인용 각주나
            # "4장의 주요 부분은…" 같은 본문 문장은 장 헤딩이 아니다 (2026-07-08)
            if re.search(r"[.!?。]\s*$", cand_title) or re.match(r"^[의을를이가은는과와도에]\s", cand_title):
                pass
            else:
                num, title = int(m.group(1)), cand_title
        if num is None:
            m = re.match(r"^chapter\s+(\d{1,2})\s*[\.:：\-–—]?\s*(.*)$", s, re.I)
            if m:
                num, title = int(m.group(1)), m.group(2)
        if num is None:
            m = re.match(r"^(\d{1,2})\s+([A-Z][A-Z0-9 ,:;()/'&\\-]{3,})$", s)
            if m:
                num, title = int(m.group(1)), m.group(2)
        if num is None:
            m = re.match(r"^(\d{1,2})[.)]\s+(.{2,90})$", s)
            if m:
                cand_title = m.group(2).strip()
                bad_inline = re.search(r"[.!?。]$|[;\"“”]|https?://|\b(?:19|20)\d{2}\b|\d+[)]", cand_title)
                ko_title = bool(re.search(r"[가-힣]", cand_title))
                ko_section = bool(re.search(r"서론|결론|개요|연구|방법|결과|논의|고찰|배경|목적|정책|윤리|분석|차원|사례|제언|나가는 말", cand_title))
                en_section = bool(re.match(r"[A-Z][A-Za-z0-9 /,:&'\\-]{2,90}$", cand_title))
                if generic_numbered_allowed and not bad_inline and not re.match(r"^\d", cand_title) and ((ko_title and ko_section) or (not ko_title and en_section)):
                    num, title = int(m.group(1)), cand_title
        if num is None:
            # "1 Introduction" 스타일 — 점 없는 영문 번호 헤딩 (저널 논문, 2026-07-03)
            # 오탐은 numbered_heading_split의 연속 번호(1→2→3)·간격 검증이 걸러낸다.
            m = re.match(r"^(\d{1,2})\s+([A-Z].{2,90})$", s)
            if m:
                cand_title = m.group(2).strip()
                bad_inline = re.search(r"[.!?。]$|[;\"“”]|https?://|\b(?:19|20)\d{2}\b|\d+[)]", cand_title)
                en_only = bool(re.match(r"[A-Z][A-Za-z0-9 /,:&'\\-]{2,90}$", cand_title))
                if generic_numbered_allowed and not bad_inline and en_only and len(cand_title.split()) <= 12:
                    num, title = int(m.group(1)), cand_title
        if num is None and generic_numbered_allowed:
            # 단독 번호 줄 + 다음 짧은 제목 줄 — 한국어 신서 스타일 (2026-07-07)
            #   "01\n\n왜 인공지능윤리인가" 처럼 번호와 제목이 다른 줄인 경우.
            # 두 자리 표기("01".."30")만 인정 — 한 자리 "1"은 각주 마커·쪽번호와
            # 구분 불가(s43681 소속 각주 오탐). 쪽번호("36" 등) 오탐은
            # 연속 번호(01→02→03)+간격 검증이 걸러낸다.
            m = re.match(r"^(\d{2})$", s)
            if m and int(m.group(1)) <= MAX_CHAPTERS:
                for _pos2, _line2 in _lines[_li + 1:_li + 4]:
                    t2 = re.sub(r"\s+", " ", _line2).strip()
                    if not t2:
                        continue
                    if (4 <= len(t2) <= 60 and re.match(r"^[가-힣A-Za-z]", t2)
                            and not re.search(r"[.!?。]\s*$|\d\s*$", t2)):
                        num, title = int(m.group(1)), t2
                    break
        if num is None or not (1 <= num <= MAX_CHAPTERS):
            continue
        title = _clean_heading_title(title)
        if not title:
            title = f"{num}장" if re.search(r"장", s) else f"Chapter {num}"
        if title.lower() in _NOTE_SECTION or len(title) > 90:
            continue
        # 끝 쪽번호가 붙은 줄 = 러닝헤더·목차 항목일 가능성 (스캔책은 매 쪽
        # "1장 제목 21" 머리글이 찍힘). 시퀀스 중단 판정에서 제외한다 (2026-07-08)
        pageno = bool(re.search(r"\s\d{1,4}\s*$", s))
        candidates.append({"num": num, "title": title, "pos": pos, "line": s, "pageno": pageno})
    return candidates


def numbered_heading_split(txt: str):
    """Fallback: split sequential Korean/English numbered headings, with or without TOC."""
    candidates = _heading_candidates(txt)
    if len(candidates) < 3:
        return None
    min_gap = 800 if len(txt) < 120_000 else 1600
    best = None
    for start_idx, start in enumerate(candidates):
        if start["num"] != 1:
            continue
        seq = [start]
        prev = start
        expected = 2
        for cand in candidates[start_idx + 1:]:
            if cand["pos"] <= prev["pos"] + min_gap:
                continue
            if cand["num"] == expected:
                seq.append(cand)
                prev = cand
                expected += 1
            elif cand["num"] == 1 and len(seq) < 3 and not cand.get("pageno"):
                # 진짜 "1장" 헤딩 재등장 = 잘못된 시작점. 단 쪽번호 달린 줄은
                # 1장 러닝헤더일 뿐이므로 시퀀스를 죽이지 않는다 (2026-07-08)
                break
        if len(seq) < 3:
            continue
        gaps = [seq[i + 1]["pos"] - seq[i]["pos"] for i in range(len(seq) - 1)]
        if min(gaps) < min_gap:
            continue
        coverage = min(len(txt), seq[-1]["pos"] + max(gaps[-1], min_gap)) - seq[0]["pos"]
        score = len(seq) * 10_000 + min(coverage // 1000, 500) - seq[0]["pos"] // 200_000
        if seq[0].get("pageno"):
            score -= 5_000   # 쪽번호 달린 시작 앵커 = 목차 항목·러닝헤더 의심 → 감점 (2026-07-08)
        if not best or score > best[0]:
            best = (score, seq)
    if not best:
        return None
    hits = best[1]
    positions = [h["pos"] for h in hits]
    tail = txt[positions[-1]:]
    end = len(txt)
    m_tail = re.search(r"(?m)^\s*(ACKNOWLEDGMENTS?|REFERENCES|BIBLIOGRAPHY|APPENDIX|참고문헌|미주|주석)\s*$", tail, re.I)
    if m_tail:
        end = positions[-1] + m_tail.start()
    else:
        end = _notes_start(txt, positions[-1] + 3000)
    bounds = positions + [end]
    chapters = []
    for idx, hit in enumerate(hits):
        body = txt[bounds[idx]:bounds[idx + 1]].strip()
        if len(body) < 300 and idx:
            if chapters:
                chapters[-1] = (chapters[-1][0], chapters[-1][1] + "\n\n" + body)
            continue
        title = _title_case_if_english(hit["title"])
        chapters.append((f"{hit['num']}. {title}", body))
    return chapters if len(chapters) >= 3 else None


# ── LLM 장 경계 판정 폴백 (2026-07-07) ─────────────────────
# 정규식 캐스케이드가 못 잡는 비정형 장 구조("첫 번째 이야기", OCR 오탈자,
# 임의 표기)를 LLM이 의미로 판정한다. 본문 전체가 아니라 짧은 줄 후보 목록만
# 보내고(비용 최소), 반환된 줄 위치로 로컬에서 분할한다(환각 무해화).

TOC_SPLIT_PROMPT = """아래는 책 본문에서 추출한 짧은 줄 후보 목록입니다 (형식: 번호 | 문서내위치% | 줄 내용).
이 중에서 **각 장(chapter)의 본문이 실제로 시작되는 최상위 장 제목 줄**만 골라내세요.

규칙:
- 목차(차례) 항목은 제외 — 보통 문서 앞쪽(위치 0~5%)에 몰려 있고 쪽수가 붙음
- 쪽번호·러닝헤더(여러 번 반복되는 책제목/장제목)·소제목·본문 조각·부록의 인용 조항 제외
- OCR 오탈자가 있어도 장 제목이면 포함 (예: '첫번때이야기' = 첫 번째 이야기)
- 같은 장 제목이 여러 위치에 보이면 본문이 시작되는 위치의 것을 선택
- 문서 처음부터 끝까지(위치 0~100%) 훑어 **모든 최상위 장을 빠짐없이** 포함 — 일부 장의
  헤딩이 OCR로 손상돼 목록에 없으면 그 장은 건너뛰되, 목록에 있는 장은 놓치지 말 것
- 최상위 장 구조가 명확하지 않으면 빈 배열을 반환

[출력] JSON only: {{"chapters": [{{"idx": <후보 번호>, "title": "<정돈된 장 제목>"}}]}} — 읽기 순서대로.

[후보 {n}줄]
{listing}"""

_LLM_SPLIT_KW = re.compile(r"\d|장|이야기|마당|부|편|강|chapter|part|lecture", re.I)
# 강한 장 신호 — 후보 초과 시 이 줄들은 절대 버리지 않는다 (2026-07-07)
_LLM_SPLIT_KW_STRONG = re.compile(
    r"이야기|마당|프롤로그|에필로그|서론|결론|들어가며|나가며|chapter|part\b|lecture"
    r"|(?:^|\s)제?\s*\d{1,2}\s*[장부편](?:\s|$|[.:：)\-])", re.I)


def llm_toc_split(txt: str):
    """정규식 전부 실패 시 LLM으로 장 시작 줄 판정. [(title, body)] 또는 None."""
    try:
        prov, _m = llm.wiki_provider_model()
        if not llm.has_key(prov):
            return None
    except Exception:
        return None
    total = len(txt)
    if total < 20_000:            # 짧은 문서는 단일장 흐름이 맞음
        return None
    cands: list[tuple[int, str]] = []
    pos = 0
    for raw in txt.splitlines(True):
        s = re.sub(r"\s+", " ", raw.rstrip("\r\n")).strip()
        if 2 <= len(s) <= 60 and not re.search(r"[.?!。…,]$", s):
            cands.append((pos, s))
        pos += len(raw)
    MAXC = 600
    if len(cands) > MAXC:         # 과다 시 헤딩스러운 줄만 (숫자·장/이야기 키워드)
        kw_only = [(p, s) for p, s in cands if _LLM_SPLIT_KW.search(s)]
        cands = kw_only if len(kw_only) >= 10 else cands
    if len(cands) > MAXC:
        # 앞에서부터 자르면 문서 뒷부분 장이 통째로 누락된다 (갓생살기 사례).
        # 강한 장 신호 줄은 전부 보존하고, 나머지로 문서 전체를 고르게 채운다.
        strong = [c for c in cands if _LLM_SPLIT_KW_STRONG.search(c[1])]
        weak = [c for c in cands if not _LLM_SPLIT_KW_STRONG.search(c[1])]
        if len(strong) > MAXC:
            step = len(strong) / MAXC
            strong = [strong[int(i * step)] for i in range(MAXC)]
        room = MAXC - len(strong)
        if room > 0 and weak:
            step = max(1, len(weak) // room)
            strong += weak[::step][:room]
        cands = sorted(set(strong), key=lambda x: x[0])
    if len(cands) < 3:
        return None
    listing = "\n".join(f"{i} | {int(p / total * 100):>2}% | {s}"
                        for i, (p, s) in enumerate(cands))
    try:
        data = _gen_json(TOC_SPLIT_PROMPT.format(n=len(cands), listing=listing), 4096)
    except Exception:
        return None
    rows = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not (3 <= len(rows) <= MAX_CHAPTERS):
        return None
    picks: list[tuple[int, str]] = []
    for r in rows:
        try:
            i = int(r.get("idx"))
        except Exception:
            return None
        if not (0 <= i < len(cands)):
            return None
        title = str(r.get("title") or cands[i][1]).strip()[:90]
        picks.append((cands[i][0], title))
    picks.sort(key=lambda x: x[0])
    dedup: list[tuple[int, str]] = []
    for p, t in picks:            # 최소 간격 1500자 — 목차 항목·중복 위치 제거
        if dedup and p - dedup[-1][0] < 1500:
            continue
        dedup.append((p, t))
    if len(dedup) < 3:
        return None
    positions = [p for p, _t in dedup]
    end = _notes_start(txt, positions[-1] + 3000)
    bounds = positions + [end]
    chapters: list[tuple[str, str]] = []
    for k, (_p, t) in enumerate(dedup):
        body = txt[bounds[k]:bounds[k + 1]].strip()
        if len(body) < 300 and chapters:
            chapters[-1] = (chapters[-1][0], chapters[-1][1] + "\n\n" + body)
            continue
        chapters.append((f"{k + 1}. {t}", body))
    return chapters if len(chapters) >= 3 else None


def chapter_split(md_text: str, txt_text: str = None, pdf_path=None):
    """(mode, [(title, body)]). mode=bookmark|visual|heading|toc|numbered|llm|single.

    Tier 0 (pdf_path 있을 때): 북마크 메타데이터 → 연결된 공급자의 PDF 시각
    판독(차례 페이지). 텍스트 층이 OCR로 열화돼도 동작 — 형식 추측이 불필요한
    가장 정확한 경로 (2026-07-07). 이후 기존 정규식 → 텍스트 LLM 폴백."""
    if pdf_path and txt_text:
        try:
            from services.toc import pdf_chapter_split
            r = pdf_chapter_split(txt_text, pdf_path)
            if r:
                return r
        except Exception:
            pass
    if md_text:
        chs = heading_chapters(_strip_noise(md_text))
        if chs:
            return "heading", chs
    for src in (txt_text, md_text):     # 목차 복원은 줄바꿈 보존된 TXT 우선
        if not src:
            continue
        chs = toc_split(_strip_noise(src))
        if chs and len(chs) >= 3:
            return "toc", chs
        chs = numbered_heading_split(_strip_noise(src))
        if chs and len(chs) >= 3:
            return "numbered", chs
    # 최후 폴백: LLM 장 경계 판정 (호출 1회로 제한)
    src = txt_text or md_text
    if src:
        chs = llm_toc_split(_strip_noise(src))
        if chs and len(chs) >= 3:
            return "llm", chs
    return "single", None


# ── 챕터 노트 생성 ──
CHAPTER_PROMPT = """당신은 신학·인문학 학술 사서입니다. 아래는 책 『{book}』의 한 장 「{chapter}」의 전문입니다.
이 장을 충실하고 깊이 있게 대표하는 옵시디언 노트를 작성하세요.

[작성 원칙]
- 반드시 한국어로만. 중국어·영어 문장 금지(고유명사 원어 병기 허용). 평서형 학술체(~한다/~이다), 높임말 금지.
- ⭐ 저자가 *다루는 주제*만 나열하지 말고 **저자의 실제 주장·논거·결론**을 서술. "~를 모색한다/다룬다/분석한다"로 끝내지 말 것.
- 이 장의 핵심 개념 정의, 논증 흐름, 근거·사례를 구체적으로. 책의 다른 장이나 무관한 주제는 끌어들이지 말 것.
- ⭐ 전문 용어는 처음 나올 때 한글 번역(원어) 순서로 병기 — 예: 대신함(substitution), 말함(le Dire). 이후에는 한글만 쓴다.
- ⭐ 해당 분야 훈련이 없는 독자도 따라오도록 풀어 쓴다: 한 문장에 한 개념, 긴 문장은 나누고, 어려운 개념은 일상어로 한 번 더 설명한다.
- ⭐ 저자가 전제하고 대화하는 앞선 사상가의 개념(예: 후설의 지향성, 하이데거의 존재 이해)은 먼저 그 개념이 무엇인지 한 문장으로 소개한 뒤에 저자의 비판·변형을 서술한다.
- OCR 노이즈·판권·목차 등 본문 외 요소 제외.
- 인용은 이 장 본문에 실제로 있는 깨끗한 문장 {n_cite}개를 정확히 그대로(지어내기 금지).

[출력] 아래 JSON으로만:
{{
  "summary": "이 장의 핵심을 2~3문장으로(저자의 주장·결론)",
  "author": "책의 저자 이름(본문·서지에서 확인될 때만. 확실치 않으면 빈 문자열)",
  "body": "## 개요\\n(이 장에서 저자가 무엇을 주장하며 어떤 결론에 이르는지 3~4문장)\\n\\n## 주요 내용\\n### (소제목 {n_sub}개 안팎)\\n(각 소제목마다 저자의 주장·논거·개념정의 3~5문장)\\n\\n## 핵심 인용\\n| 주제 | 인용(본문 그대로) |\\n|---|---|\\n\\n## 핵심 키워드\\n(이 장의 핵심 개념 5~8개. 한 줄에 하나씩 '#키워드 — 개념 해설 1~2문장' 형식. 키워드는 공백 없는 한국어, 원어는 해설 쪽에. 예: #대신함 — 주체가 타자의 책임을 자기 것으로 짊어져 타자의 자리에 서는 것(substitution). 저자는 이것을 주체성의 근원으로 본다.)"
}}

===장 전문===
{text}
===끝==="""

def _gen_json(prompt, max_out):
    prov, model = llm.wiki_provider_model()
    return llm.complete_json(prov, model, "", prompt, max_tokens=max_out)

def generate_chapter(book, chap_title, chap_text):
    prov, _ = llm.wiki_provider_model()
    max_in = llm.MAX_INPUT_CHARS.get(prov, 500_000)
    chars = len(chap_text)
    n_sub  = min(10, max(3, chars // 9000))
    n_cite = min(8, max(3, chars // 12000))
    max_out = min(30000, max(8192, n_sub * 2400))
    data = _gen_json(CHAPTER_PROMPT.format(
        book=book, chapter=chap_title, text=chap_text[:max_in], n_sub=n_sub, n_cite=n_cite), max_out)
    if data.get("body"):
        data["body"], _, _ = gw.rebuild_citations(data["body"], chap_text, [], chap_title, target=n_cite)
    return data

OVERVIEW_PROMPT = """다음은 책 『{book}』를 장별로 요약한 것입니다. 이를 바탕으로 책 전체 개요를 쓰세요.
한국어만·평서체·저자의 핵심 주장과 결론 중심. 장 요약에 없는 내용 지어내기 금지.
전문 용어는 처음 나올 때 한글 번역(원어) 병기. 해당 분야 훈련이 없는 독자도 읽도록 쉬운 문장으로 풀어 쓴다.
[출력] JSON only:
{{ "category":"신학자|교회|윤리|AI개념|사회학 중 하나",
   "author":"저자 이름(장 요약에서 확인될 때만. 확실치 않으면 빈 문자열)",
   "published_date":"장 요약에서 명시적으로 확인되는 출판일/발행일. YYYY-MM-DD 또는 YYYY. 확실치 않으면 빈 문자열",
   "publisher":"장 요약에서 명시적으로 확인되는 출판사/발행처. 확실치 않으면 빈 문자열",
   "summary":"책 전체 2~3문장 요약",
   "intro":"(저자의 핵심 주장과 책 전체 논지를 4~6문장으로. 머리말 '## 책 개요' 없이 본문만)",
   "keywords":"책 전체 핵심 개념 5~8개. 한 줄에 하나씩 '#키워드 — 개념 해설 1~2문장' 형식(줄바꿈 \\n 구분). 키워드는 공백 없는 한국어, 원어는 해설 쪽에" }}
[장별 요약]
{secs}"""

def generate_overview(book, sections):
    secs = "\n".join(f"{s['idx']}. {s['title']}: {s['summary']}" for s in sections)
    try:
        return _gen_json(OVERVIEW_PROMPT.format(book=book, secs=secs), 4096)
    except Exception:
        return {"category": "기타", "summary": "", "intro": ""}


# ── 노트 빌더 ──
def _clean_title(t: str) -> str:
    """제목 앞 장번호(예 '2. ', '02) ') 제거 — 우리가 NN. 을 따로 붙이므로 중복 방지."""
    return re.sub(r"^\s*\d+[.)]\s*", "", t).strip(" :：")

def _demote(md: str) -> str:
    """인라인 A용: 장 본문의 ## → ###, ### → #### (허브 섹션과 레벨 충돌 방지)."""
    return re.sub(r"(?m)^(#{2,5})(\s)", r"#\1\2", md)

def _chap_filename(idx, title):
    safe = re.sub(r'[/\\:*?"<>|]', ' ', title).strip()
    safe = re.sub(r"\s{2,}", " ", safe)
    if len(safe) > 60:
        safe = safe[:60].rsplit(" ", 1)[0]      # 단어 경계서 자름
    safe = safe.strip(" .,-:") or "장"           # 끝 공백·구두점 제거(옵시디언 링크 깨짐 방지)
    return f"{idx:02d}_{safe}.md"

LINK_HEADER = "## 📑 챕터 심층 노트"

def _source_frontmatter(ov=None, source_meta=None) -> str:
    ov = ov or {}
    source_meta = source_meta or {}
    return smeta.frontmatter_lines({
        "published": gw.nfc(ov.get("published_date", "")),
        "publisher": gw.nfc(ov.get("publisher", "")),
        **source_meta,
    })

def chapter_note_md(book, stem, s, cat, ov=None, source_meta=None):
    today = datetime.date.today().isoformat()
    fm = ("---\n" f"title: {s['title']}\n" f"book: {book}\n" f"chapter: {s['idx']}\n"
          f"category: {cat}\n" f"summary: {s['summary']}\n"
          + _source_frontmatter(ov, source_meta)
          + f"model: {llm.wiki_provider_model()[1]}\n" f"generated: {today}\n" "---\n\n")
    src = gw.make_filename(stem)[:-3]
    return (fm + f"# {s['idx']:02d}. {s['title']}\n\n" + s["body"].strip()
            + f"\n\n## 출처\n- 책 허브: [[{src}]]\n")

def build_links_block(stem, items):
    out = [LINK_HEADER, ""]
    for idx, title, fname, summary in items:
        out.append(f"### {idx:02d}. {title}")
        if summary:
            out.append(summary)
        out.append(f"→ [[{stem}/{fname[:-3]}|전문 보기]]")
        out.append("")
    return "\n".join(out).rstrip() + "\n"

def append_or_replace_links(a_path, stem, block):
    text = a_path.read_text(encoding="utf-8")
    if LINK_HEADER in text:
        text = re.sub(re.escape(LINK_HEADER) + r".*?(?=\n## |\Z)", block.rstrip(), text, flags=re.DOTALL)
    else:
        text = text.rstrip() + "\n\n" + block
    a_path.write_text(text, encoding="utf-8")

def hub_a_note(book, stem, cat, ov, items, source_meta=None):
    today = datetime.date.today().isoformat()
    fm = ("---\n" f"title: {book}\n" f"category: {cat}\n"
          f"summary: {gw.nfc(ov.get('summary',''))}\n"
          + _source_frontmatter(ov, source_meta)
          + f"model: {llm.wiki_provider_model()[1]}\n" f"generated: {today}\n" "mode: chapter(A+B)\n" "---\n\n")
    intro = gw.nfc(ov.get("intro", "")).strip()
    return fm + f"# {book}\n\n## 책 개요\n{intro}\n\n" + build_links_block(stem, items)

def inline_a_note(book, cat, ov, sections, source_meta=None):
    """기본 A: 한 노트에 책 개요 + 각 장을 풍성한 ## 섹션으로 인라인."""
    today = datetime.date.today().isoformat()
    fm = ("---\n" f"title: {book}\n" f"category: {cat}\n"
          f"summary: {gw.nfc(ov.get('summary',''))}\n"
          + _source_frontmatter(ov, source_meta)
          + f"model: {llm.wiki_provider_model()[1]}\n" f"generated: {today}\n" "mode: chapter(A)\n" "---\n\n")
    parts = [fm + f"# {book}\n", "## 책 개요", gw.nfc(ov.get("intro", "")).strip(), ""]
    for s in sections:
        parts.append(f"## {s['idx']:02d}. {s['title']}")
        parts.append(_demote(s["body"].strip()))
        parts.append("")
    return "\n".join(parts)


# ── 책 처리 ──
def wiki_note_exists(stem: str) -> bool:
    """WIKI_DIR에 이 책의 노트가 이미 있는지 확인 (파일명 기준)."""
    target_fn = gw.make_filename(gw.nfc(stem))
    return any(md.name == target_fn for md in gw.OUT_DIR.rglob("*.md"))

def find_all_pending(regen: bool = False):
    """PROCESSED_DIR의 모든 .txt 중 위키 미생성 목록. regen=True면 전체 반환."""
    all_txts = sorted(gw.SRC_DIR.glob("*.txt"), key=lambda p: p.stat().st_size)
    if regen:
        return all_txts
    return [f for f in all_txts if not wiki_note_exists(f.stem)]

def _single_pass(stem):
    """장 구조 없는 책 → gemini_wiki 단일 노트."""
    txt = find_txt(stem)
    if not txt:
        raise RuntimeError(f"단일 폴백 실패 — TXT 없음: {stem}")
    data = gw.generate(txt)
    out = gw.write_note(data, txt)
    return {"mode": "single", "a": str(out)}

def process_book(stem, mode="auto"):
    """mode: auto(장구조 있으면 A, 없으면 single) / A(인라인) / full(허브+B) / add(B+링크)."""
    md = find_layout_md(stem); txt = find_txt(stem)
    md_text = md.read_text(encoding="utf-8", errors="ignore") if md else None
    txt_text = txt.read_text(encoding="utf-8", errors="ignore") if txt else None
    if not md_text and not txt_text:
        raise RuntimeError(f"소스 없음: {stem} (재처리 필요)")
    smode, chapters = chapter_split(md_text, txt_text)
    if mode == "auto":
        if smode == "single" or not chapters:
            return _single_pass(stem)
        mode = "A"
    if smode == "single" or not chapters:
        print("   ⚠️ 진짜 장 구조 없음 → 단일 노트로", flush=True)
        return _single_pass(stem)
    book = gw.nfc(stem)
    print(f"   📚 {book}: {len(chapters)}장 (분할={smode}, mode={mode})", flush=True)
    sections = []
    for i, (ct, cb) in enumerate(chapters, 1):
        title = _clean_title(gw.nfc(ct))
        print(f"      [{i}/{len(chapters)}] {title[:40]} ({len(cb):,}자)…", flush=True)
        d = generate_chapter(book, title, cb)
        sections.append({"idx": i, "hint": ct, "title": title,
                         "summary": gw.nfc(d.get("summary", "")), "body": gw.nfc(d.get("body", ""))})
    ov = generate_overview(book, sections)
    cat = (ov.get("category") or "기타").split("|")[0].strip()
    source_meta = smeta.pdf_dates_for_txt(txt) if txt else {}
    out_cat = gw.OUT_DIR / cat
    out_cat.mkdir(parents=True, exist_ok=True)
    a_path = out_cat / gw.make_filename(book)
    if mode == "A":
        a_path.write_text(inline_a_note(book, cat, ov, sections, source_meta), encoding="utf-8")
        return {"mode": "A", "chapters": len(sections), "cat": cat, "a": str(a_path)}
    # full / add → 장별 B 노트
    bookdir = out_cat / book
    bookdir.mkdir(parents=True, exist_ok=True)
    items = []
    for s in sections:
        fn = _chap_filename(s["idx"], s["title"])
        (bookdir / fn).write_text(chapter_note_md(book, book, s, cat, ov, source_meta), encoding="utf-8")
        items.append((s["idx"], s["title"], fn, s["summary"]))
    if mode == "add":
        if not a_path.exists():
            raise RuntimeError(f"A 노트 없음: {a_path} (먼저 A 생성)")
        append_or_replace_links(a_path, book, build_links_block(book, items))
        return {"mode": "add", "chapters": len(sections), "cat": cat, "a": str(a_path), "b": len(items)}
    a_path.write_text(hub_a_note(book, book, cat, ov, items, source_meta), encoding="utf-8")
    return {"mode": "full", "chapters": len(sections), "cat": cat, "a": str(a_path), "b": len(items)}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stem",  help="책 파일명(확장자 제외)")
    g.add_argument("--file",  help="txt 파일 경로")
    g.add_argument("--all",   action="store_true", help="미생성 책 전체 일괄 처리")
    g.add_argument("--regen", action="store_true", help="이미 생성된 책도 전체 재처리")
    ap.add_argument("--mode", default="auto", choices=["auto", "A", "full", "add"])
    args = ap.parse_args()

    if args.all or args.regen:
        targets = find_all_pending(regen=args.regen)
        prov, model = llm.wiki_provider_model()
        print(f"🚀 챕터 위키 일괄 생성 [{prov}:{model}] — 대상 {len(targets)}권", flush=True)
        ok = fail = 0
        for i, t in enumerate(targets, 1):
            stem = gw.nfc(t.stem)
            print(f"[{i}/{len(targets)}] 📖 {stem} ({t.stat().st_size // 1024}KB)", flush=True)
            t0 = time.time()
            try:
                r = process_book(stem, args.mode)
                gw.mark_done(gw.nfc(t.name))
                print(f"   ✅ {r}  ({int(time.time()-t0)}초)", flush=True)
                ok += 1
            except Exception as e:
                print(f"   ❌ 실패: {type(e).__name__}: {str(e)[:300]}", flush=True)
                fail += 1
            time.sleep(1)
        print(f"=== 완료 {ok}권 / 실패 {fail}권 ===", flush=True)
        return

    stem = args.stem or gw.nfc(Path(args.file).stem)
    t0 = time.time()
    r = process_book(stem, args.mode)
    print(f"   ✅ {r}  ({int(time.time()-t0)}초)", flush=True)

if __name__ == "__main__":
    main()
