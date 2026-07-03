"""장별 분할·합치기·요약 — chapters/<책>/ 폴더 단위 처리."""

import json
import re as _re
from datetime import date
from pathlib import Path

import config as cfg
import llm_providers as llm

from services.common import TXT_SUB, _nfc, append_log
from services.files import find_md, find_txt, txt_dir
from services.translate import _split_paragraphs_robust

DONE_DIR = cfg.DONE_DIR


# ─── 챕터 요약 파일 (_wiki.md — 2026-07-03 JSON→MD 전환) ──────────
# LLM 출력은 complete_json으로 형식을 강제하되, 디스크에는 사람이 읽고
# 위키반영 전에 손으로 고칠 수 있는 고정 형식 MD로 저장한다.
# 구형 _wiki.json은 읽기 폴백으로만 지원 (재요약 시 삭제).

_SUMMARY_PREFIX = "> **요약:**"


def _format_summary_md(book: str, chapter: str, summary: str, body: str) -> str:
    _p, model = llm.wiki_provider_model()
    one_line = " ".join((summary or "").split())
    return (
        "---\n"
        f"book: {book}\n"
        f"chapter: {chapter}\n"
        f"model: {model}\n"
        f"generated: {date.today().isoformat()}\n"
        "---\n"
        f"{_SUMMARY_PREFIX} {one_line}\n\n"
        f"{(body or '').strip()}\n"
    )


def parse_summary_md(text: str) -> tuple[str, str]:
    """고정 형식 _wiki.md → (summary, body). 손으로 수정된 파일도 관대하게 파싱:
    요약 줄이 없으면 summary=''이고 전체가 body가 된다."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    lines = text.lstrip("\n").splitlines()
    summary = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip().startswith(_SUMMARY_PREFIX):
            summary = ln.strip()[len(_SUMMARY_PREFIX):].strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return summary, body


def load_summary_file(path: Path) -> dict | None:
    """요약 파일(_wiki.md 또는 구형 _wiki.json) → {"summary","body"}. 실패 시 None."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix.lower() == ".json":
            d = json.loads(text)
            return d if isinstance(d, dict) else None
        summary, body = parse_summary_md(text)
        if not (summary or body):
            return None
        return {"summary": summary, "body": body}
    except Exception:
        return None


def summary_file_for(ch_path: Path) -> Path | None:
    """챕터 TXT에 대응하는 요약 파일 — _wiki.md 우선, 구형 _wiki.json 폴백."""
    md = ch_path.with_name(ch_path.stem + "_wiki.md")
    if md.exists():
        return md
    js = ch_path.with_name(ch_path.stem + "_wiki.json")
    return js if js.exists() else None


def list_summary_files(ch_dir: Path) -> list[Path]:
    """책 챕터 폴더의 요약 파일 목록 — 같은 챕터는 _wiki.md가 구형 json을 대체."""
    if not ch_dir.exists():
        return []
    by_stem: dict[str, Path] = {}
    for f in ch_dir.glob("*_wiki.json"):
        by_stem[f.stem] = f
    for f in ch_dir.glob("*_wiki.md"):
        by_stem[f.stem] = f
    return [by_stem[k] for k in sorted(by_stem)]


def chapters_dir(ws_name: str, stem: str) -> Path:
    return DONE_DIR / ws_name / "chapters" / stem


def _single_chapter_name(stem: str) -> str:
    safe = _re.sub(r'[/\\:*?"<>|]', " ", stem).strip()[:50].strip(" .,:-")
    return f"01_{safe or '본문'}.txt"


def _is_small_document_for_whole_translation(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    paragraphs = _split_paragraphs_robust(sample, target_chunk=1800, min_para=4)
    return len(sample) <= 120_000 and len(paragraphs) <= 14


def _write_single_chapter_from_text(ws_name: str, stem: str, text: str) -> tuple[Path, bool]:
    ch_dir = chapters_dir(ws_name, stem)
    ch_dir.mkdir(parents=True, exist_ok=True)
    for old in ch_dir.glob("*"):
        if old.is_file():
            try:
                old.unlink()
            except Exception:
                pass
    ch_path = ch_dir / _single_chapter_name(stem)
    ch_path.write_text(text, encoding="utf-8")
    return ch_path, True


def list_done_books() -> list[tuple[str, str, Path]]:
    """(ws, stem, txt_path) — done 폴더의 모든 책 TXT (1_txt/ 우선, 루트 fallback)."""
    books: list[tuple[str, str, Path]] = []
    if not DONE_DIR.exists():
        return books
    for ws_dir in sorted(DONE_DIR.iterdir()):
        if not ws_dir.is_dir() or ws_dir.name.startswith("_"):
            continue
        ws = ws_dir.name
        seen: set[str] = set()
        txt_sub = ws_dir / TXT_SUB
        if txt_sub.exists():
            for txt in sorted(txt_sub.glob("*.txt")):
                s = _nfc(txt.stem)
                if s not in seen:
                    books.append((ws, s, txt)); seen.add(s)
        for txt in sorted(ws_dir.glob("*.txt")):
            s = _nfc(txt.stem)
            if s not in seen:
                books.append((ws, s, txt)); seen.add(s)
    return books


def split_book_to_chapters(ws_name: str, stem: str, allow_short: bool = False) -> tuple[int, str]:
    """장 분리 실행. 챕터 TXT 파일 저장. (저장 수, 오류 메시지) 반환."""
    try:
        import chapter_wiki as _cw
    except ImportError:
        return 0, "chapter_wiki 임포트 실패"
    txt_p = find_txt(DONE_DIR, ws_name, stem)
    md_p  = find_md(DONE_DIR, ws_name, stem)
    md_text  = md_p.read_text(encoding="utf-8", errors="ignore")  if md_p  else None
    txt_text = txt_p.read_text(encoding="utf-8", errors="ignore") if txt_p else None
    if not md_text and not txt_text:
        return 0, "TXT/MD 파일 없음"
    source_text = txt_text or md_text or ""
    if _is_small_document_for_whole_translation(source_text) and not allow_short:
        return 0, "짧은 문서 감지"
    mode, chapters = _cw.chapter_split(md_text, txt_text)
    if (mode == "single" or not chapters) and allow_short:
        ch_path, _ = _write_single_chapter_from_text(ws_name, stem, source_text)
        return 1, f"단일장으로 저장됨 → {ch_path.name}"
    if mode == "single" or not chapters:
        return 0, "장 구조 감지 안 됨 — 단일 본문입니다 (기존 위키 생성 탭을 쓰세요)"
    ch_dir = chapters_dir(ws_name, stem)
    ch_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for i, (title, body) in enumerate(chapters, 1):
        safe = _re.sub(r'[/\\:*?"<>|]', ' ', title).strip()[:50].strip(" .,:-")
        (ch_dir / f"{i:02d}_{safe}.txt").write_text(body, encoding="utf-8")
        saved += 1
    return saved, ""


def _merge_chapter_folder(ws_name: str, stem: str, prefer_ko: bool = False) -> tuple[bool, Path | None, str]:
    """챕터 폴더를 하나의 TXT로 다시 합친다. prefer_ko=True면 각 챕터의 _ko.txt 우선."""
    ch_dir = chapters_dir(ws_name, stem)
    if not ch_dir.exists():
        return False, None, "챕터 폴더 없음"
    chapters = sorted(
        [f for f in ch_dir.glob("??_*.txt") if not f.stem.endswith(("_ko", "_wiki"))],
        key=lambda p: p.name,
    )
    if not chapters:
        return False, None, "합칠 챕터가 없음"
    out_dir = txt_dir(DONE_DIR, ws_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (f"{stem}__merged_ko.txt" if prefer_ko else f"{stem}__merged.txt")
    parts: list[str] = [f"# {stem}", ""]
    used_ko = 0
    for ch in chapters:
        body_path = ch.with_name(ch.stem + "_ko.txt") if prefer_ko and ch.with_name(ch.stem + "_ko.txt").exists() else ch
        if body_path != ch:
            used_ko += 1
        title = _re.sub(r"^\d+_", "", ch.stem)
        parts += [f"## {title}", body_path.read_text(encoding="utf-8", errors="ignore").strip(), ""]
    out_path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")
    return True, out_path, f"{len(chapters)}개 챕터 합침" + (f" · 번역본 {used_ko}개 사용" if used_ko else "")


def summarize_one_chapter(ch_path: Path, book_stem: str) -> tuple[bool, str]:
    """단일 챕터 TXT → 요약 생성 후 _wiki.md 저장. (ok, summary snippet)."""
    try:
        import chapter_wiki as _cw
    except ImportError:
        return False, "chapter_wiki 임포트 실패"
    try:
        ko_path = ch_path.with_name(ch_path.stem + "_ko.txt")
        src = (ko_path if ko_path.exists() else ch_path).read_text(encoding="utf-8", errors="ignore")
        chap_title = _re.sub(r"^\d+_", "", ch_path.stem)
        data = _cw.generate_chapter(book_stem, chap_title, src)
        if not isinstance(data, dict):
            raise RuntimeError("요약 응답이 JSON 객체가 아님")
        if not (data.get("summary") and data.get("body")):
            keys = ", ".join(sorted(map(str, data.keys()))) or "없음"
            raise RuntimeError(f"요약 응답 필드 부족(summary/body 없음, keys={keys})")
        (ch_path.with_name(ch_path.stem + "_wiki.md")).write_text(
            _format_summary_md(book_stem, chap_title, data["summary"], data["body"]),
            encoding="utf-8",
        )
        legacy = ch_path.with_name(ch_path.stem + "_wiki.json")
        if legacy.exists():           # 재요약 시 구형 json 정리 (md가 대체)
            try:
                legacy.unlink()
            except Exception:
                pass
        return True, (data.get("summary") or "")[:120]
    except Exception as e:
        msg = str(e)[:300]
        try:
            append_log(f"ERROR: 장별 요약 실패 - {ch_path.name} ({type(e).__name__}) {msg}")
        except Exception:
            pass
        return False, msg[:200]
