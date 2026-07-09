#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""기존 위키 노트 형식 개선 워커 (2026-07-09).

보관함(WIKI_DIR)의 완성 노트를 원문 재투입 없이 새 노트 규약으로 개선한다:
(1) 용어 한글(원어) 병기, (2) 풀어 쓰기, (3) 선행 사상가 개념 소개,
(4) author frontmatter, (5) '#키워드 — 해설' 섹션.

재개 가능 설계:
- 완료 노트는 frontmatter `refined:` 표식 → 재실행 시 자동 건너뜀.
- LLM 사용량 리밋 감지 시 20분 대기 후 같은 노트부터 재시도(최대 12시간).
- 개선본이 원본의 60% 미만이거나 섹션 구조가 사라지면 저장하지 않고 원본 유지.
- 전문/번역 수록 노트(_번역, 35,000자 초과)와 부실 노트(800자 미만)는 패스.

실행은 위키반영 앱의 [노트 형식 개선] 버튼(services.wiki.trigger_note_retrofit)이
담당한다 — 앱 프로세스가 외장 볼륨 권한을 이어준다. 단독 실행도 가능.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfg            # noqa: E402
import llm_providers as llm    # noqa: E402

WIKI = cfg.WIKI_DIR
LOG_NAME = "_retrofit.log"
LIMIT_RE = re.compile(r"rate.?limit|429|too many|usage.?limit|quota|exhaust|overloaded", re.I)
MAX_LIMIT_WAITS = 36          # 20분 × 36 = 12시간까지 리밋 해제 대기

PROMPT = """아래는 이미 완성된 옵시디언 책 노트입니다. **내용(주장·논거·수치·인용)은 그대로 보존**하면서 형식만 다음 기준으로 개선하세요.

[개선 기준]
1. 전문 용어가 처음 나올 때 한글 번역(원어) 병기 — 원어가 확실히 알려진 경우만. 지어내기 금지.
2. 긴 문장은 나누고 어려운 개념은 일상어로 한 번 더 풀어 쓴다. 정보 추가·삭제 금지.
3. 저자가 전제하는 앞선 사상가·이론의 개념이 언급되면 한 문장으로 그 개념을 소개한 뒤 서술.
4. author: 노트에서 확인되는 이 책/문서의 저자 이름 (확실치 않으면 빈 문자열).
5. keywords: 기존 tags를 바탕으로 '#키워드 — 이 노트 내용에 근거한 개념 해설 1~2문장' 형식 5~8개.
   한 줄에 하나씩, **모든 키워드에 해설 필수**, 키워드는 공백 없는 한국어, 원어는 해설 쪽에.
6. 노트의 섹션 구조(##, ###)와 제목은 유지. 표·인용문은 그대로 보존.

[출력] JSON only: {{"author": "...", "body": "(개선된 본문 전체 — frontmatter 제외, '# 제목'부터. '## 핵심 키워드' 섹션 없이)", "keywords": "(한 줄씩 #키워드 — 해설)"}}

===노트===
{note}
===끝==="""


def log(msg: str) -> None:
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def split_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[:end + 4], text[end + 4:]
    return "", text


def classify(text: str, stem: str) -> str | None:
    """개선 대상이면 None, 아니면 제외 사유를 반환. UI 통계와 워커가 공유."""
    fm, body = split_frontmatter(text)
    if not fm or "model:" not in fm:
        return "제외(파이프라인 노트 아님)"
    if "refined:" in fm:
        return "건너뜀(이미 개선됨)"
    if "## 핵심 요약" not in body and "## 주요 내용" not in body:
        return "제외(요약 노트 구조 아님)"
    if "_번역" in stem or len(body) > 35_000:
        return "제외(전문/번역 수록 노트)"
    if len(body.strip()) < 800:
        return "제외(부실 노트 — 원문 재생성 대상)"
    return None


def list_candidates(wiki: Path) -> list[Path]:
    out = []
    for f in wiki.rglob("*.md"):
        rel = f.relative_to(wiki)
        if any(part.startswith((".", "_")) for part in rel.parts):
            continue                      # .trash·.obsidian·_retrofit.log 등
        # 보관함 안에 복사된 중첩 금고(.obsidian 가진 하위 폴더)의 사본은 제외 (2026-07-09)
        d = f.parent
        nested = False
        while d != wiki:
            if (d / ".obsidian").exists():
                nested = True
                break
            d = d.parent
        if not nested:
            out.append(f)
    return sorted(out)


def improve(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    reason = classify(text, path.stem)
    if reason:
        return reason
    fm, body = split_frontmatter(text)

    prov, model = llm.wiki_provider_model()
    data = llm.complete_json(prov, model, "", PROMPT.format(note=text), max_tokens=20000)
    new_body = (data.get("body") or "").strip()
    keywords = "\n".join(ln.strip() for ln in (data.get("keywords") or "").splitlines() if ln.strip().startswith("#"))
    author = (data.get("author") or "").strip()

    # 내용 유실 방어 — 개선본이 원본보다 크게 줄면 원본 유지
    if len(new_body) < max(600, int(len(body.strip()) * 0.6)) or "## " not in new_body:
        return "실패(검증 미달 — 원본 유지)"
    new_body = re.sub(r"\n## 핵심 키워드\n[\s\S]*$", "", new_body).rstrip()
    if keywords:
        new_body += "\n\n## 핵심 키워드\n" + keywords

    used = llm.effective_wiki_model()
    head = fm.rstrip()
    head = head[:-3].rstrip("\n")
    if author and not re.search(r"(?m)^author:", head):
        head += f"\nauthor: {author}"
    head += f"\nrefined: {used} ({time.strftime('%Y-%m-%d')})\n---\n"

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(head + "\n" + new_body + "\n", encoding="utf-8")
    tmp.replace(path)
    return "OK"


def main() -> None:
    files = list_candidates(WIKI)
    log(f"=== 노트 형식 개선 시작 — 후보 {len(files)}개 (보관함: {WIKI}) ===")
    counts: dict[str, int] = {}
    consec_fail = 0        # 연속 CLI 실패 = 메시지 못 잡은 사용량 리밋으로 간주
    for i, f in enumerate(files, 1):
        waits = 0
        while True:
            try:
                r = improve(f)
                if not r.startswith("실패"):
                    consec_fail = 0
                break
            except Exception as e:
                m = str(e)
                suspect_limit = LIMIT_RE.search(m) or ("CLI exit" in m and consec_fail >= 3)
                if suspect_limit and waits < MAX_LIMIT_WAITS:
                    waits += 1
                    log(f"⏸ 리밋 감지({waits}/{MAX_LIMIT_WAITS}) — 20분 대기 후 재시도: {f.name} | {m[-140:]}")
                    time.sleep(1200)
                    continue
                consec_fail += 1
                r = "실패(오류)"
                log(f"⚠️ 오류: {f.name} — {m[-200:]}")
                break
        counts[r] = counts.get(r, 0) + 1
        mark = "✅" if r == "OK" else ("·" if r.startswith(("건너뜀", "제외")) else "⚠️")
        log(f"({i}/{len(files)}) {mark} {r}: {f.name}")
    log(f"=== 노트 형식 개선 종료 — {counts} ===")


if __name__ == "__main__":
    main()
