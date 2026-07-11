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
    """'drop'=버림(제어/개행), 'space'=공백, ''=실제 글자."""
    if ch in ("\r", "\n", "\t") or unicodedata.category(ch)[0] == "C":
        return "drop"
    if ch == " " or ch == "￾" or unicodedata.category(ch)[0] == "Z":
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
        if not is_sp:
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
    """실제 공백 문자 + x간격(보조)으로 띄어쓰기 복원."""
    chars = sorted(chars, key=lambda c: c[0])
    out, prev_x1, pending = [], None, False
    for x0, _y, x1, ch, is_sp in chars:
        if is_sp:
            pending = True
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


def _detect_boundaries(w, gl):
    """열 사이 세로 빈 띠(거터)들의 중심 x 목록. N단 지원. (공백 제외)"""
    real = [c for c in gl if not c[4]]
    if len(real) < 40:
        return []
    BIN = 4
    nb = int(w // BIN) + 2
    cov = [0] * nb
    for x0, _y, x1, _c, _s in real:
        for k in range(int(x0 // BIN), int(x1 // BIN) + 1):
            if 0 <= k < nb:
                cov[k] += 1
    peak = max(cov) or 1
    occ = [k for k in range(nb) if cov[k] > peak * 0.02]
    if not occ:
        return []
    xL, xR = occ[0], occ[-1]
    thr = peak * 0.10
    MIN_W = 3                              # 최소 ~12pt 폭
    bounds, k = [], xL + 1
    while k < xR:
        if cov[k] <= thr:
            s = k
            while k < xR and cov[k] <= thr:
                k += 1
            if (k - s) >= MIN_W:
                bounds.append((s + k) / 2 * BIN)
        else:
            k += 1
    return bounds


def _reading_order(page):
    w, h, gl, mh, mw = _glyphs(page)
    if not gl:
        return ""
    bounds = _detect_boundaries(w, gl)
    rows = _group_rows(gl, mh * 0.5)
    # 공백 임계값은 이 페이지의 실제 간격 분포에서 적응형으로 추정(폰트 무관).
    space_gap = _adaptive_space_gap(rows, mw)
    ncol = len(bounds) + 1
    cols = [[] for _ in range(ncol)]
    out = []

    def col_of(x):
        i = 0
        while i < len(bounds) and x >= bounds[i]:
            i += 1
        return i

    def flush():
        if any(cols):
            for c in cols:
                out.extend(c); out.append("")
                c.clear()

    for row in rows:
        row.sort(key=lambda c: c[0])
        real = [c for c in row if not c[4]]
        if not real:
            continue
        if not bounds:
            out.append(_text(row, space_gap)); continue
        # 이 행이 어떤 경계를 '틈 없이' 가로지르면 전체폭으로 간주
        fullwidth = False
        for b in bounds:
            lefts = [c[2] for c in real if c[2] <= b]
            rights = [c[0] for c in real if c[0] >= b]
            if lefts and rights and (min(rights) - max(lefts)) < mh * 1.5:
                fullwidth = True
                break
        if fullwidth:
            flush(); out.append(_text(row, space_gap)); continue
        # 열별로 분배
        buckets = {}
        for c in row:
            buckets.setdefault(col_of((c[0] + c[2]) / 2), []).append(c)
        for ci in sorted(buckets):
            cols[ci].append(_text(buckets[ci], space_gap))
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
