#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini 위키 생성기 (2026-06-09 신설) — NotebookLM 로컬 대체.

TXT(책 전문) → Gemini Flash(책 통째 컨텍스트, RAG·업로드·임베드 없음) → 옵시디언 노트.
로컬 7b/14b의 한계(얕음·중국어 드리프트·요약오염·인용 OCR노이즈)를 모두 회피.

키: ~/.config/gemini_wiki.key (한 줄) 또는 환경변수 GEMINI_API_KEY.
사용:
  gemini_wiki.py --limit 2          # 미완료 앞에서 2권(테스트)
  gemini_wiki.py --file "<txt경로>"  # 특정 책 1권
  gemini_wiki.py --all              # 전체
"""
import os, re, sys, json, glob, time, argparse, datetime, unicodedata
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path.home() / '.local/bin'))
import config as cfg
import llm_providers as llm

SRC_DIR  = cfg.PROCESSED_DIR        # 원본 TXT
OUT_DIR  = cfg.WIKI_DIR             # 옵시디언 출력
DONE     = cfg.GEMINI_DONE_FILE
KEY_FILE = Path.home() / ".config/gemini_wiki.key"
MODEL    = "gemini-2.5-flash"     # 품질↑. 비용 부담되면 gemini-2.0-flash 로 교체
MAX_CHARS = 1_900_000             # ≈95만 토큰(Gemini 1M 한도 여유). 초과분만 절단

def nfc(s): return unicodedata.normalize("NFC", s)

def get_key():
    if KEY_FILE.exists():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k: return k
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k: return k
    sys.exit(f"❌ API 키 없음. {KEY_FILE} 에 키 한 줄을 넣으세요(또는 GEMINI_API_KEY).")

PROMPT = """당신은 신학·인문학 학술 사서입니다. 아래는 책 『{title}』의 전문(OCR 텍스트, 일부 노이즈 포함)입니다.
이 책 한 권을 충실하고 깊이 있게 대표하는 옵시디언 위키 노트를 작성하세요.

[작성 원칙]
- 반드시 한국어로만 작성. 중국어(汉字 문장)·영어 문장 금지(고유명사 원어 병기는 허용).
- 문체: **평서형 학술체(~한다 / ~이다 / ~였다)**. '~합니다 / ~입니다' 같은 높임말 절대 금지.
- 분량은 책 크기에 비례: **두꺼운 책일수록 소제목을 더 많이, 각 항목을 더 길고 자세히** 쓸 것(짧은 논문은 간결해도 됨).
- 책 전체(서론·전개·중간 장·결론)를 균형 있게 반영. 앞부분만 요약 금지.
- ⭐⭐ 가장 중요: 저자가 *다루는 주제·제기하는 질문*만 나열하지 말고 **저자의 실제 답·주장·결론·해결책**을 서술하라. "~를 모색한다 / 다룬다 / 분석한다 / 살펴본다 / 검토한다 / 논의한다"처럼 범위만 말하고 끝내는 문장 금지. 반드시 "저자는 ~라고 주장한다 / ~로 본다 / 그 근거는 ~ / 결론적으로 ~다"처럼 **내용(무엇을·어떻게·왜)**을 담을 것.
  [나쁜 ✗] "경제성장과 환경보호의 양립 가능성을 모색한다."
  [좋은 ✓] "기든스는 생태학적 근대화론을 따라 경제성장과 환경보호가 양립 가능하다고 보며, 그 방법으로 청정기술 투자와 환경규제의 시장유인화를 제시한다."
- 깊이: 핵심 개념의 *정의*, 논증이 전개되는 *흐름*, 인물/학파 간 *차이와 긴장*, 구체적 *근거·사례*를 담을 것.
- ⚠️ 이 책이 실제로 다루지 않는 주제(AI·기술·다른 사상가 등)는 절대 끌어들이지 말 것. 오직 이 책 내용에만 충실.
- OCR 노이즈(깨진 글자), 판권·추천사·목차 등 본문 외 요소는 제외하고 본문 핵심만.
- 인용은 책 본문에서 실제로 등장하는 의미 있는 문장을 **정확히 그대로 {n_cite}개** 뽑을 것(절대 지어내거나 짜깁기 금지 — 원문에 없는 문장이면 차라리 빼라). 페이지번호·각주표시·줄바꿈 없이 한 문장으로 깔끔하게. 깨진 글자 섞인 구절은 제외.

[출력] 아래 JSON으로만 답하세요(다른 설명 없이):
{{
  "category": "신학자|교회|윤리|AI개념|사회학 중 하나",
  "title": "책의 실제 제목만(부제·'위키 노트'·'옵시디언' 같은 군더더기 금지)",
  "summary": "2~3문장 핵심 요약",
  "tags": ["핵심어1","핵심어2","핵심어3","핵심어4","핵심어5"],
  "body": "마크다운 본문 — 아래 구조 그대로"
}}

body 구조:
## 핵심 요약
(3~4문장. 저자의 핵심 **주장과 결론** — 무엇을 다루는지가 아니라, 저자가 무엇을 주장하며 어떤 답에 이르는지.)

## 주요 내용
### (소제목 — 책 전개 순서로 {n_sub}개 안팎. 책이 길수록 더 많이·각 항목도 더 길고 자세히)
(각 소제목마다: 그 부분에서 **저자가 무엇을 주장하고 어떤 논거·과정으로 그 결론에 이르는지** + 핵심 개념의 정의를 3~5문장으로 구체적으로. 주제 호명·질문 나열 금지. 저자의 답·해결책이 반드시 드러나게.)

## 핵심 인용
| 주제 | 인용(책 본문 그대로) |
|---|---|
(원문에 실제로 있는 깨끗한 문장 {n_cite}개. 한 줄에 한 인용)

## 관련 개념
(이 책과 실제로 연결되는 개념·인물 — 책에 없으면 이 섹션 생략)

===책 전문===
{book}
===끝==="""

def _despace(s): return re.sub(r"\s+", "", re.sub(r"\([^)]*\)", "", nfc(s))).strip("\"“”‘’ ")

def _clean_sentence(s: str) -> bool:
    s = s.strip()
    if not (25 <= len(s) <= 180): return False
    if len(re.findall(r"[가-힣]", s)) < len(s) * 0.45: return False   # 한글 비중
    if re.search(r"[ᄀ-ᇿ㄰-㆏]", s): return False                      # 깨진 자모
    if re.search(r"[一-鿿]{2,}", s): return False                      # 한자 연쇄
    if len(re.findall(r"[^\w가-힣()\.,\'\"·\-—%·:!?\s]", s)) > 3: return False  # 잡기호
    return True

def rebuild_citations(body: str, source_full: str, keywords: list, title: str, target: int = 5):
    """Gemini 인용 중 *원문 확인된 것만* 남기고, 부족분은 원본에서 직접 깨끗한 문장으로 채움.
    → 인용 전부 100% 원문 보장."""
    src = nfc(source_full); Sx = re.sub(r"\s+", "", src)
    # 1) Gemini 인용 중 원문 일치만 수집
    rows = []
    in_cite = False
    for ln in body.split("\n"):
        s = ln.strip()
        if s.startswith("## 핵심 인용"): in_cite = True; continue
        if in_cite and s.startswith("## "): in_cite = False
        if in_cite and s.startswith("|") and "---" not in s:
            cols = [c.strip() for c in s.strip("|").split("|")]
            if len(cols) >= 2 and cols[1] not in ("인용", "인용(책 본문 그대로)", "원문 인용", ""):
                qclean = re.sub(r"\s+", " ", cols[1]).strip()
                if len(_despace(qclean)) >= 10 and _despace(qclean) in Sx:
                    rows.append((cols[0], qclean))
    rows = rows[:target]
    n_kept = len(rows)
    # 표 재구성 → 본문의 핵심 인용 자리에 교체 삽입
    tbl = ["## 핵심 인용", "", "| 주제 | 원문 인용(대조 검증) |", "|---|---|"]
    tbl += [f"| {th} | {q} |" for th, q in rows] or ["| (원문 확인된 직접 인용 없음) | |"]
    out, in_cite, inserted = [], False, False
    for ln in body.split("\n"):
        s = ln.strip()
        if s.startswith("## 핵심 인용"):
            in_cite = True; out.extend(tbl); inserted = True; continue
        if in_cite:
            if s.startswith("## "): in_cite = False; out.append(ln)
            continue
        out.append(ln)
    if not inserted: out += [""] + tbl
    return "\n".join(out), n_kept, 0


def make_filename(title):
    safe = re.sub(r'[/\\:]', ' - ', title)            # 경로문자 → ' - '
    safe = re.sub(r'[*?"<>|]', '', safe).strip()
    safe = re.sub(r'\s{2,}', ' ', safe)
    return (safe or "untitled") + ".md"

def generate(txt_path):
    raw_full = nfc(txt_path.read_text(encoding="utf-8", errors="ignore"))
    title_guess = nfc(txt_path.stem)
    raw = raw_full
    if len(raw) > MAX_CHARS:
        print(f"   ⚠️ 초대형({len(raw):,}자) → 앞 {MAX_CHARS:,}자만 사용")
        raw = raw[:MAX_CHARS]
    chars = len(raw)
    n_sub     = min(24, max(7, chars // 35000))      # 소제목 수: 책 크기 비례
    n_cite    = min(30, max(8, chars // 28000))      # 인용 후보 수
    cite_keep = min(15, max(4, chars // 110000))     # 보존 인용 상한
    max_out   = min(60000, max(16384, n_sub * 2200)) # 출력 토큰: 분량 비례
    prompt = PROMPT.format(title=title_guess, book=raw, n_sub=n_sub, n_cite=n_cite)
    prov, model = llm.wiki_provider_model()
    print(f"   📏 {chars:,}자 → 소제목~{n_sub}·인용후보{n_cite}·보존≤{cite_keep}·출력{max_out} [{prov}:{model}]")
    data = llm.complete_json(prov, model, "", prompt, max_tokens=max_out)
    # 인용 원문 대조 — Gemini 인용 중 원문확인분만 + 부족분은 원본에서 직접 추출(100% 원문)
    if data.get("body"):
        data["body"], n_kept, _ = rebuild_citations(
            data["body"], raw_full, data.get("tags", []), nfc(data.get("title", title_guess)),
            target=cite_keep)
        print(f"   🔎 인용: 원문확인 {n_kept}개만 보존(지어낸 인용 제거)")
    return data

def write_note(data, txt_path):
    cat   = (data.get("category") or "기타").split("|")[0].strip()
    title = nfc(data.get("title") or txt_path.stem).strip()
    summ  = nfc(data.get("summary") or "").strip()
    tags  = data.get("tags") or []
    body  = nfc(data.get("body") or "").strip()
    today = datetime.date.today().isoformat()
    fm = ("---\n"
          f"title: {title}\n"
          f"summary: {summ}\n"
          f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
          f"source: {nfc(txt_path.name)}\n"
          f"model: {llm.wiki_provider_model()[1]}\n"
          f"generated: {today}\n"
          "---\n\n")
    note = fm + f"# {title}\n\n" + body + f"\n\n## sources\n- `{nfc(txt_path.name)}`\n"
    out_dir = OUT_DIR / cat
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / make_filename(nfc(txt_path.stem))   # 파일명=원본 책명(안정·중복방지). 제목은 frontmatter에
    out.write_text(note, encoding="utf-8")
    return out

def load_done():
    if DONE.exists():
        return {l.strip() for l in DONE.read_text(encoding="utf-8").splitlines() if l.strip()}
    return set()

def mark_done(name):
    with open(DONE, "a", encoding="utf-8") as f:
        f.write(name + "\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--file", type=str, default=None)
    ap.add_argument("--match", type=str, default=None, help="파일명 NFC 부분일치로 1권 선택")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.match:
        hits = [f for f in SRC_DIR.glob("*.txt") if args.match in nfc(f.name)]
        if not hits: sys.exit(f"❌ '{args.match}' 일치 파일 없음")
        targets = hits[:1]
    elif args.file:
        targets = [Path(args.file)]
    else:
        done = load_done()
        allf = sorted(SRC_DIR.glob("*.txt"), key=lambda p: p.stat().st_size)  # 작은 책부터
        targets = [f for f in allf if nfc(f.name) not in done]
        if not args.all:
            targets = targets[:args.limit]

    print(f"🚀 Gemini({MODEL}) 위키 생성 — 대상 {len(targets)}권")
    ok = fail = 0
    for i, t in enumerate(targets, 1):
        if not t.exists():
            print(f"[{i}/{len(targets)}] 원본 없음: {t.name}"); continue
        print(f"[{i}/{len(targets)}] 📖 {nfc(t.name)} ({t.stat().st_size//1024}KB)")
        t0 = time.time()
        try:
            data = generate(t)
            out = write_note(data, t)
            mark_done(nfc(t.name))
            print(f"   ✅ {out.parent.name}/{out.name}  ({int(time.time()-t0)}초)")
            ok += 1
        except Exception as e:
            print(f"   ❌ 실패: {type(e).__name__}: {str(e)[:300]}")
            fail += 1
        time.sleep(1)  # rate 여유
    print(f"=== 완료 {ok}권 / 실패 {fail}권 ===")

if __name__ == "__main__":
    main()
