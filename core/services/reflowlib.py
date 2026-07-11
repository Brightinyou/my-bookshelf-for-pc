# -*- coding: utf-8 -*-
"""추출 텍스트 후처리: 반복 머리말/쪽번호/세로텍스트 제거 + 문장 reflow.
pdfcols(좌표 추출)와 pdftotext 폴백 양쪽에서 공용으로 쓴다."""
import re
from collections import Counter

_HANGUL = re.compile(r"[가-힣]")


def strip_page_furniture(pages):
    """반복 머리말/꼬리말·쪽번호·세로(회전) 텍스트를 제거한 라인 리스트."""
    # 1) 페이지 가장자리에서 반복되는 머리말/꼬리말 수집
    edges = Counter()
    for pg in pages:
        ne = [l.strip() for l in pg.split("\n") if l.strip()]
        for l in ne[:2] + ne[-2:]:
            edges[l] += 1
    thr = max(3, len(pages) // 2)
    repeated = {l for l, n in edges.items() if n >= thr and len(l) < 90}

    flat = []
    for pg in pages:
        flat.extend(pg.split("\n"))

    # 2) 전체 스트림에서 다시 드러난 반복 줄 집계(컬럼분리·회전으로 조각난 것 포함)
    freq = Counter(l.strip() for l in flat if l.strip())

    out = []
    seen = set()   # 반복 콘텐츠 줄은 '첫 등장만' 유지 → 제목/저자가 러닝헤더로도
                   # 반복될 때, 첫 페이지의 진짜 제목은 살리고 이후 헤더만 제거한다.
    for l in flat:
        s = l.strip()
        if not s:
            out.append("")
            continue
        if re.fullmatch(r"\d{1,4}", s):                 # 단독 쪽번호 → 항상 제거
            continue
        if _is_vertical_noise(s):                       # 세로(회전) 텍스트 흔적
            continue
        if (s in repeated) or (len(s) < 90 and freq[s] >= 3):
            if s in seen:
                continue                                # 두 번째 이후 = 러닝헤더/꼬리말
            seen.add(s)                                 # 첫 등장 = 실제 콘텐츠로 유지
        out.append(l)
    return out


def _is_vertical_noise(s: str) -> bool:
    """회전된 세로 텍스트는 글자마다 공백이 낀
    'V o l . : ( 0 1 2 )' 또는 한 글자짜리 줄로 나오는 경향이 있다."""
    toks = s.split()
    if len(toks) >= 6 and sum(len(t) for t in toks) / len(toks) <= 1.3:
        return True
    return False


def reflow(text: str) -> str:
    """물리적 줄바꿈을 문장/문단 단위로 재결합."""
    paras = re.split(r"\n[ \t]*\n", text)
    out = []
    for para in paras:
        rows = [r.strip() for r in para.split("\n") if r.strip()]
        if not rows:
            continue
        buf = ""
        for row in rows:
            if not buf:
                buf = row
                continue
            prev, nxt = buf[-1], row[0]
            if prev == "-":
                if nxt.islower():                       # coop-\neration → cooperation
                    buf = buf[:-1] + row
                else:                                   # High-\nLevel → High-Level
                    buf += row
            else:
                # 한글 포함 모든 스크립트: 공백 결합이 가장 안전.
                # (한글은 어절 경계 줄바꿈이 흔해 붙이면 단어가 뭉친다)
                buf += " " + row
        out.append(buf)
    return "\n\n".join(out)


def clean_default_text(raw: str) -> str:
    """폼피드(\\f)로 페이지가 나뉜 raw 텍스트 → 정리된 본문 (pdftotext 폴백용)."""
    pages = raw.split("\f")
    lines = strip_page_furniture(pages)
    return reflow("\n".join(lines))
