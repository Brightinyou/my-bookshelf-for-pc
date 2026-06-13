#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""챕터 모드 위키 생성기 (2026-06-09). gemini_wiki.py 보완 모듈.

긴 책을 '진짜 장 구조'가 있을 때만 장별로 생성(제목은 원전 그대로, 작명 금지).
분할 캐스케이드: ①Docling MD ## 헤딩 → ②인쇄된 목차(TOC) 복원 → ③둘 다 없으면 단일.
출력: A(한 노트 허브) + B(장별 개별 노트). 모드 full / add-chapters / (A단독은 추후).
"""
import os, re, sys, json, time, datetime, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path.home() / ".local/bin"))
import config as cfg
import gemini_wiki as gw   # nfc, rebuild_citations, make_filename, OUT_DIR, get_key
import llm_providers as llm

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
def find_docling_md(stem: str):
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
    return None

def _strip_noise(md: str) -> str:
    md = _IMG_RE.sub("", md)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)
    return md.replace("\x0c", "\n")

def _is_note_title(t: str) -> bool:
    s = t.strip().lower()
    return bool(_NOTE_TITLE_RE.match(t.strip())) or s in _NOTE_SECTION


# ── ① Docling ## 헤딩 기반(의미 단위) ──
def heading_chapters(md: str):
    lines = md.split("\n")
    segs, cur = [], {"title": "서두", "lines": []}
    for ln in lines:
        m = _HEAD_RE.match(ln)
        if m and not _is_note_title(m.group(2)):
            if cur["lines"] or segs:
                segs.append(cur)
            cur = {"title": m.group(2).strip(), "lines": [ln]}
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

def toc_split(txt: str):
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


def chapter_split(md_text: str, txt_text: str = None):
    """(mode, [(title, body)]). 헤딩=MD(##), 목차=TXT(줄바꿈 보존) 우선. mode=heading|toc|single."""
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
    return "single", None


# ── 챕터 노트 생성 ──
CHAPTER_PROMPT = """당신은 신학·인문학 학술 사서입니다. 아래는 책 『{book}』의 한 장 「{chapter}」의 전문입니다.
이 장을 충실하고 깊이 있게 대표하는 옵시디언 노트를 작성하세요.

[작성 원칙]
- 반드시 한국어로만. 중국어·영어 문장 금지(고유명사 원어 병기 허용). 평서형 학술체(~한다/~이다), 높임말 금지.
- ⭐ 저자가 *다루는 주제*만 나열하지 말고 **저자의 실제 주장·논거·결론**을 서술. "~를 모색한다/다룬다/분석한다"로 끝내지 말 것.
- 이 장의 핵심 개념 정의, 논증 흐름, 근거·사례를 구체적으로. 책의 다른 장이나 무관한 주제는 끌어들이지 말 것.
- OCR 노이즈·판권·목차 등 본문 외 요소 제외.
- 인용은 이 장 본문에 실제로 있는 깨끗한 문장 {n_cite}개를 정확히 그대로(지어내기 금지).

[출력] 아래 JSON으로만:
{{
  "summary": "이 장의 핵심을 2~3문장으로(저자의 주장·결론)",
  "body": "## 개요\\n(이 장에서 저자가 무엇을 주장하며 어떤 결론에 이르는지 3~4문장)\\n\\n## 주요 내용\\n### (소제목 {n_sub}개 안팎)\\n(각 소제목마다 저자의 주장·논거·개념정의 3~5문장)\\n\\n## 핵심 인용\\n| 주제 | 인용(본문 그대로) |\\n|---|---|"
}}

===장 전문===
{text}
===끝==="""

def _gen_json(prompt, max_out):
    prov, model = llm.wiki_provider_model()
    return llm.complete_json(prov, model, "", prompt, max_tokens=max_out)

def generate_chapter(book, chap_title, chap_text):
    chars = len(chap_text)
    n_sub  = min(10, max(3, chars // 9000))
    n_cite = min(8, max(3, chars // 12000))
    max_out = min(30000, max(8192, n_sub * 2400))
    data = _gen_json(CHAPTER_PROMPT.format(
        book=book, chapter=chap_title, text=chap_text[:600000], n_sub=n_sub, n_cite=n_cite), max_out)
    if data.get("body"):
        data["body"], _, _ = gw.rebuild_citations(data["body"], chap_text, [], chap_title, target=n_cite)
    return data

OVERVIEW_PROMPT = """다음은 책 『{book}』를 장별로 요약한 것입니다. 이를 바탕으로 책 전체 개요를 쓰세요.
한국어만·평서체·저자의 핵심 주장과 결론 중심. 장 요약에 없는 내용 지어내기 금지.
[출력] JSON only:
{{ "category":"신학자|교회|윤리|AI개념|사회학 중 하나",
   "summary":"책 전체 2~3문장 요약",
   "intro":"(저자의 핵심 주장과 책 전체 논지를 4~6문장으로. 머리말 '## 책 개요' 없이 본문만)" }}
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

def chapter_note_md(book, stem, s, cat):
    today = datetime.date.today().isoformat()
    fm = ("---\n" f"title: {s['title']}\n" f"book: {book}\n" f"chapter: {s['idx']}\n"
          f"category: {cat}\n" f"summary: {s['summary']}\n"
          f"model: {llm.wiki_provider_model()[1]}\n" f"generated: {today}\n" "---\n\n")
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

def hub_a_note(book, stem, cat, ov, items):
    today = datetime.date.today().isoformat()
    fm = ("---\n" f"title: {book}\n" f"category: {cat}\n"
          f"summary: {gw.nfc(ov.get('summary',''))}\n"
          f"model: {llm.wiki_provider_model()[1]}\n" f"generated: {today}\n" "mode: chapter(A+B)\n" "---\n\n")
    intro = gw.nfc(ov.get("intro", "")).strip()
    return fm + f"# {book}\n\n## 책 개요\n{intro}\n\n" + build_links_block(stem, items)

def inline_a_note(book, cat, ov, sections):
    """기본 A: 한 노트에 책 개요 + 각 장을 풍성한 ## 섹션으로 인라인."""
    today = datetime.date.today().isoformat()
    fm = ("---\n" f"title: {book}\n" f"category: {cat}\n"
          f"summary: {gw.nfc(ov.get('summary',''))}\n"
          f"model: {llm.wiki_provider_model()[1]}\n" f"generated: {today}\n" "mode: chapter(A)\n" "---\n\n")
    parts = [fm + f"# {book}\n", "## 책 개요", gw.nfc(ov.get("intro", "")).strip(), ""]
    for s in sections:
        parts.append(f"## {s['idx']:02d}. {s['title']}")
        parts.append(_demote(s["body"].strip()))
        parts.append("")
    return "\n".join(parts)


# ── 책 처리 ──
LONG_BOOK_CHARS = 300_000   # 이 이상 + 진짜 장구조면 auto 모드가 챕터 생성

def _single_pass(stem):
    """진짜 장구조 없거나 짧은 책 → gemini_wiki 단일 노트."""
    txt = find_txt(stem)
    if not txt:
        raise RuntimeError(f"단일 폴백 실패 — TXT 없음: {stem}")
    data = gw.generate(txt)
    out = gw.write_note(data, txt)
    return {"mode": "single", "a": str(out)}

def process_book(stem, mode="auto"):
    """mode: auto(길고 장구조면 A, 아니면 single) / A(인라인) / full(허브+B) / add(B+링크)."""
    md = find_docling_md(stem); txt = find_txt(stem)
    md_text = md.read_text(encoding="utf-8", errors="ignore") if md else None
    txt_text = txt.read_text(encoding="utf-8", errors="ignore") if txt else None
    if not md_text and not txt_text:
        raise RuntimeError(f"소스 없음: {stem} (재처리 필요)")
    total = len(txt_text or md_text)
    smode, chapters = chapter_split(md_text, txt_text)
    if mode == "auto":
        if smode == "single" or not chapters or total < LONG_BOOK_CHARS:
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
    out_cat = gw.OUT_DIR / cat
    out_cat.mkdir(parents=True, exist_ok=True)
    a_path = out_cat / gw.make_filename(book)
    if mode == "A":
        a_path.write_text(inline_a_note(book, cat, ov, sections), encoding="utf-8")
        return {"mode": "A", "chapters": len(sections), "cat": cat, "a": str(a_path)}
    # full / add → 장별 B 노트
    bookdir = out_cat / book
    bookdir.mkdir(parents=True, exist_ok=True)
    items = []
    for s in sections:
        fn = _chap_filename(s["idx"], s["title"])
        (bookdir / fn).write_text(chapter_note_md(book, book, s, cat), encoding="utf-8")
        items.append((s["idx"], s["title"], fn, s["summary"]))
    if mode == "add":
        if not a_path.exists():
            raise RuntimeError(f"A 노트 없음: {a_path} (먼저 A 생성)")
        append_or_replace_links(a_path, book, build_links_block(book, items))
        return {"mode": "add", "chapters": len(sections), "cat": cat, "a": str(a_path), "b": len(items)}
    a_path.write_text(hub_a_note(book, book, cat, ov, items), encoding="utf-8")
    return {"mode": "full", "chapters": len(sections), "cat": cat, "a": str(a_path), "b": len(items)}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stem")
    g.add_argument("--file")
    ap.add_argument("--mode", default="auto", choices=["auto", "A", "full", "add"])
    args = ap.parse_args()
    stem = args.stem or gw.nfc(Path(args.file).stem)
    t0 = time.time()
    r = process_book(stem, args.mode)
    print(f"   ✅ {r}  ({int(time.time()-t0)}초)", flush=True)

if __name__ == "__main__":
    main()
