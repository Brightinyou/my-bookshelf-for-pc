# -*- coding: utf-8 -*-
"""pypdfium2 좌표 기반 다단(N단) → 읽기순서 추출.
- 이미 의존성에 있는 pypdfium2(Apache/BSD) 사용 → PyMuPDF(AGPL) 불필요.
- 띄어쓰기는 실제 공백 문자 유지 + x좌표 간격 보조(깨진 공백 폰트 대응).
- 각 행을 거터(빈 세로 띠)에서 열별로 분할해 컬럼 읽기순서 복원.
논문 2단, 뉴스레터, 1단→2단 혼합, 한글+영어 혼합, 3단 등을 처리한다."""
import statistics
import unicodedata

import pypdfium2 as pdfium

from services import reflowlib


def _kind(ch):
    """'drop'=버림(제어/개행), 'space'=공백, 'broken'=폰트 미매핑 글리프, ''=실제 글자."""
    if ch in ("￾", "￿"):
        # 폰트가 유니코드로 매핑 못한 글리프. 이 문서에선 줄끝 하이픈, 다른
        # 문서에선 공백으로도 쓰인다 → 위치(줄끝/중간)로 _text에서 해석한다.
        return "broken"
    if ch in ("\r", "\n", "\t") or unicodedata.category(ch)[0] == "C":
        return "drop"
    if ch == " " or unicodedata.category(ch)[0] == "Z":
        return "space"
    return ""


def _glyphs(page):
    """(x0, ycen, x1, ch, is_space) 목록 + 페이지크기 + 자높이/폭 중앙값.
    실제 공백 문자는 살려두고(is_space=True), 폭이 깨진 폰트를 위해
    x간격 기반 보조 판정과 병행한다."""
    w, h = page.get_size()
    tp = page.get_textpage()
    n = tp.count_chars()
    if n == 0:
        return w, h, [], 10, 6
    full = tp.get_text_range()
    gl, heights, widths = [], [], []
    for i in range(n):
        ch = full[i] if i < len(full) else ""
        if not ch:
            continue
        k = _kind(ch)
        if k == "drop":
            continue
        l, b, r, t = tp.get_charbox(i, loose=True)   # advance 기준 박스
        x0, x1 = min(l, r), max(l, r)
        y0, y1 = h - max(t, b), h - min(t, b)
        if x1 <= x0 or y1 <= y0:
            continue
        is_sp = (k == "space")
        gl.append((x0, (y0 + y1) / 2, x1, " " if is_sp else ch, is_sp))
        if not is_sp and k != "broken":     # 미매핑 글리프는 자폭/높이 통계에서 제외
            heights.append(y1 - y0); widths.append(x1 - x0)
    mh = statistics.median(heights) if heights else 10
    mw = statistics.median(widths) if widths else 6
    return w, h, gl, mh, mw


def _group_rows(gl, tol):
    """y로 행 묶기 (위→아래). 공백 포함(텍스트 복원용)."""
    gl = sorted(gl, key=lambda c: (c[1], c[0]))
    rows, cur, cy = [], [], None
    for c in gl:
        if cy is None or abs(c[1] - cy) <= tol:
            cur.append(c)
            cy = sum(x[1] for x in cur) / len(cur)
        else:
            rows.append(cur); cur = [c]; cy = c[1]
    if cur:
        rows.append(cur)
    return rows


def _text(chars, space_gap):
    """실제 공백 문자 + x간격(보조)으로 띄어쓰기 복원.
    미매핑 글리프(￾)는 줄 끝이면 하이픈(-), 줄 중간이면 공백으로 해석한다."""
    chars = sorted(chars, key=lambda c: c[0])
    n = len(chars)
    out, prev_x1, pending = [], None, False
    for idx, (x0, _y, x1, ch, is_sp) in enumerate(chars):
        if is_sp:
            pending = True
            prev_x1 = x1 if prev_x1 is None else max(prev_x1, x1)
            continue
        if ch in ("￾", "￿"):
            more = any((not chars[j][4]) and chars[j][3] not in ("￾", "￿")
                       for j in range(idx + 1, n))
            if more:
                pending = True                 # 줄 중간 → 공백
            elif out and out[-1].isascii() and out[-1].isalpha():
                out.append("-")                # 줄 끝 + 라틴 문자 뒤 → 영어 단어 분철
            # 그 외(한글 등) 줄 끝 → 아무것도 안 함(줄바꿈은 reflow가 공백으로 이음)
            prev_x1 = x1 if prev_x1 is None else max(prev_x1, x1)
            continue
        if prev_x1 is not None and (pending or (x0 - prev_x1) > space_gap):
            out.append(" ")
        out.append(ch)
        prev_x1 = x1
        pending = False
    return "".join(out).strip()


def _adaptive_space_gap(rows, mw):
    """페이지의 실제 글자 간격 분포에서 '공백' 임계값을 추정한다.
    loose 박스라 어절/단어 내부 간격은 0 이하(겹침)이고, 공백은 뚜렷이 양수다.
    폰트마다 공백 폭이 달라(예: 조밀한 한글 본문 2.8pt) 고정값은 위험하므로
    양수 간격들의 중앙값 절반을 임계로 삼아 두 무리 사이 골짜기에 둔다."""
    gaps = []
    for row in rows:
        reals = sorted([c for c in row if not c[4]], key=lambda c: c[0])
        gaps.extend(b[0] - a[2] for a, b in zip(reals, reals[1:]))
    # 단어/어절 사이 공백만 후보로 — 컬럼 거터·블록 사이 큰 간격(> mw*1.5)은 제외해야
    # 중앙값이 부풀지 않는다(2단 페이지에서 특히 중요).
    cand = sorted(g for g in gaps if mw * 0.05 < g < mw * 1.5)
    if not cand:
        return mw * 0.25
    med = cand[len(cand) // 2]
    return min(mw * 0.45, max(mw * 0.1, med * 0.5))


def _cluster(positions, tol):
    """가까운 위치들을 묶어 (중심x, 개수) 목록으로."""
    if not positions:
        return []
    positions = sorted(positions)
    groups, cur = [], [positions[0]]
    for p in positions[1:]:
        if p - cur[-1] <= tol:
            cur.append(p)
        else:
            groups.append(cur); cur = [p]
    groups.append(cur)
    return [(statistics.median(g), len(g)) for g in groups]


def _reading_order(page):
    """페이지 → 읽기순서 텍스트.
    전체폭(제목·초록)과 2단 본문이 한 페이지에 섞여도, 페이지 전역이 아니라
    '여러 행이 공유하는 넓은 세로 간격'으로 거터를 찾아 그 행들만 컬럼 분리한다.
    세로 간격이 크면 문단 경계로 보고 빈 줄을 넣어 문단 구조를 보존한다."""
    w, h, gl, mh, mw = _glyphs(page)
    if not gl:
        return ""
    rows = _group_rows(gl, mh * 0.5)
    space_gap = _adaptive_space_gap(rows, mw)
    col_gap = max(mw * 2.5, space_gap * 3, 10.0)   # 컬럼 사이 거터로 볼 최소 간격

    # 1) 각 행의 '넓은 간격'(거터 후보) 중심 x 수집 (전체폭 행은 대부분 후보 없음)
    row_reals, cands = [], []
    for row in rows:
        reals = sorted([c for c in row if not c[4]], key=lambda c: c[0])
        row_reals.append((row, reals))
        for a, b in zip(reals, reals[1:]):
            if (b[0] - a[2]) >= col_gap and w * 0.15 < (a[2] + b[0]) / 2 < w * 0.85:
                cands.append((a[2] + b[0]) / 2)

    # 2) 여러 행이 공유하는 위치만 거터로 채택 (전체폭 행의 우연한 간격 배제)
    min_support = max(3, len(rows) // 8)
    boundaries = sorted(cx for cx, n in _cluster(cands, mw * 4) if n >= min_support)

    ncol = len(boundaries) + 1
    cols = [[] for _ in range(ncol)]
    col_lasty = [None] * ncol
    out = []
    out_lasty = None                        # 전체폭/단일 컬럼 흐름의 마지막 y
    para_gap = mh * 1.8                      # 이보다 세로 간격이 크면 문단 경계

    def col_of(x):
        i = 0
        while i < len(boundaries) and x >= boundaries[i]:
            i += 1
        return i

    def flush():
        nonlocal out_lasty
        for i, c in enumerate(cols):
            if any(s.strip() for s in c):
                out.extend(c); out.append("")
            c.clear()
            col_lasty[i] = None
        out_lasty = None

    def emit_full(text, ytop):
        nonlocal out_lasty
        if out_lasty is not None and (ytop - out_lasty) > para_gap:
            out.append("")                  # 문단 경계
        out.append(text)
        out_lasty = ytop

    for row, reals in row_reals:
        if not reals:
            continue
        ytop = min(c[1] for c in reals)
        if not boundaries:
            emit_full(_text(row, space_gap), ytop); continue
        # 어떤 거터를 '틈 없이' 가로지르면 전체폭 행(제목·초록 등) → 통째로
        crosses = False
        for b in boundaries:
            l = [c[2] for c in reals if c[2] <= b]
            r = [c[0] for c in reals if c[0] >= b]
            if l and r and (min(r) - max(l)) < col_gap:
                crosses = True
                break
        if crosses:
            flush(); emit_full(_text(row, space_gap), ytop); continue
        # 열별 분배 (+ 세로 간격이 크면 문단 경계 삽입)
        buckets = {}
        for c in row:
            buckets.setdefault(col_of((c[0] + c[2]) / 2), []).append(c)
        for ci in sorted(buckets):
            if col_lasty[ci] is not None and (ytop - col_lasty[ci]) > para_gap:
                cols[ci].append("")
            cols[ci].append(_text(buckets[ci], space_gap))
            col_lasty[ci] = ytop
    flush()
    return "\n".join(out)


def pdf_to_pages(path):
    """PDF → 페이지별 읽기순서 텍스트 리스트. 반환: (pages, skipped)
    안전망①: 특정 페이지에서 예외가 나도 그 페이지만 건너뛰고 나머지는 살린다."""
    pdf = pdfium.PdfDocument(str(path))
    pages, skipped = [], 0
    try:
        for page in pdf:
            try:
                pages.append(_reading_order(page))
            except Exception:
                pages.append("")
                skipped += 1
    finally:
        pdf.close()
    return pages, skipped


def pdf_to_text(path):
    """PDF → 다단 정렬 + 머리말 제거 + 문장 reflow 된 본문. 반환: (text, skipped_pages)"""
    pages, skipped = pdf_to_pages(path)
    lines = reflowlib.strip_page_furniture(pages)
    return reflowlib.reflow("\n".join(lines)), skipped
