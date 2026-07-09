"""Wiki 생성 — Gemini 위키 트리거, 챕터 요약 → Obsidian 노트, 보관함(Vault) 관리."""

import json
import os
import re as _re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config as cfg
import llm_providers as llm

from services.chapters import (
    _author_from_stem, chapters_dir, find_overview_file, list_summary_files,
    load_overview_file, load_summary_file, summarize_book_overview,
)
from services.common import append_log, open_path

WIKI_DIR      = cfg.WIKI_DIR
RAW_DIR       = cfg.RAW_DIR
PROCESSED_DIR = cfg.PROCESSED_DIR

GEMINI_WIKI  = cfg.find_script("gemini_wiki.py")    # 2026-06-09 위키=Gemini로 교체
CHAPTER_WIKI = cfg.find_script("chapter_wiki.py")   # 2026-06-09 챕터 모드(긴 책 자동 장별)
WIKI_LOG     = cfg.WIKI_LOG_DIR


def wiki_generator_running() -> bool:
    # 윈도우: pgrep 없음 — psutil로 커맨드라인 검사 (2026-06-11 윈도우 크래시 수정)
    try:
        import psutil
        return any(
            "gemini_wiki.py" in " ".join(p.info.get("cmdline") or [])
            for p in psutil.process_iter(["cmdline"])
        )
    except Exception:
        return False


def _wiki_env(wiki_target: str | None = None) -> dict:
    """위키 생성기 자식 프로세스 환경. 사용자가 고른 보관함(Vault)이 있으면
    MYBOOKSHELF_WIKI_DIR로 전달(config.py가 WIKI_DIR로 해석). (2026-06-11)
    wiki_target: UI에서 고른 보관함 경로 — st.session_state 의존 제거 (2026-07-03)."""
    env = {**os.environ, "PYTHONUTF8": "1"}   # 윈도우 cp949에서 이모지 출력 크래시 방지
    target = (wiki_target or "").strip()
    if target and Path(target).expanduser().resolve() != WIKI_DIR.resolve():
        env["MYBOOKSHELF_WIKI_DIR"] = target
    return env


def trigger_wiki_generation(wiki_target: str | None = None) -> int:
    """미처리 책을 Gemini 위키 생성기로 일괄 생성(--all). (2026-06-09 Gemini화)
    add_pdf/raw/processed의 *.txt 중 gemini_done에 없는 것을 처리한다."""
    if wiki_generator_running():
        return 0
    if not GEMINI_WIKI.exists():
        append_log(f"ERROR: GEMINI_WIKI 부재 - {GEMINI_WIKI}")
        return 0
    log_path = WIKI_LOG / f"gemini_wiki_{datetime.now().strftime('%Y%m%d')}.log"
    try:
        env = _wiki_env(wiki_target)
        subprocess.Popen(
            [cfg.PYTHON, "-u", str(GEMINI_WIKI), "--all"],
            stdout=open(log_path, "a", encoding="utf-8"), stderr=subprocess.STDOUT,
            env=env,
        )
        append_log("Gemini Wiki 일괄 생성(--all) 트리거"
                   + (f" → 보관함(Vault) {env['MYBOOKSHELF_WIKI_DIR']}" if "MYBOOKSHELF_WIKI_DIR" in env else ""))
    except Exception as e:
        append_log(f"ERROR: gemini_wiki --all Popen 실패 ({type(e).__name__}) {str(e)[:200]}")
    return 0


# ── 기존 노트 형식 개선 (note_retrofit.py — 2026-07-09) ─────────
NOTE_RETROFIT = Path(__file__).resolve().parent.parent / "note_retrofit.py"


def note_retrofit_running() -> bool:
    try:
        import psutil
        return any(
            "note_retrofit.py" in " ".join(p.info.get("cmdline") or [])
            for p in psutil.process_iter(["cmdline"])
        )
    except Exception:
        return False


def note_retrofit_stats(wiki_target: str | None = None) -> dict:
    """보관함 노트 스캔 — {"done": 개선완료, "pending": 대상, "excluded": 제외}."""
    import note_retrofit as _nr
    wdir = Path(wiki_target).expanduser() if (wiki_target or "").strip() else WIKI_DIR
    done = pending = excluded = 0
    for f in _nr.list_candidates(wdir):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            excluded += 1
            continue
        r = _nr.classify(text, f.stem)
        if r is None:
            pending += 1
        elif r.startswith("건너뜀"):
            done += 1
        else:
            excluded += 1
    return {"done": done, "pending": pending, "excluded": excluded}


def trigger_note_retrofit(wiki_target: str | None = None) -> bool:
    """노트 형식 개선 배치를 앱 프로세스 권한으로 백그라운드 실행 (재개 안전)."""
    if note_retrofit_running():
        return False
    env = _wiki_env(wiki_target)
    wdir = Path(env.get("MYBOOKSHELF_WIKI_DIR", str(WIKI_DIR)))
    log_path = wdir / "_retrofit.log"
    try:
        subprocess.Popen(
            [cfg.PYTHON, "-u", str(NOTE_RETROFIT)],
            stdout=open(log_path, "a", encoding="utf-8"), stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        append_log(f"노트 형식 개선 배치 트리거 → {wdir}")
        return True
    except Exception as e:
        append_log(f"ERROR: note_retrofit Popen 실패 ({type(e).__name__}) {str(e)[:200]}")
        return False


def trigger_gemini_wiki(txt_path: Path, wiki_target: str | None = None) -> bool:
    """주어진 TXT(책 전문)를 Gemini 위키 생성기로 백그라운드 생성. (2026-06-09)
    RAG·임베드 없이 책 통째를 Gemini에 넣어 옵시디언 노트를 만든다."""
    if not txt_path or not Path(txt_path).exists():
        append_log(f"WARN: Gemini wiki — TXT 없음 ({txt_path})")
        return False
    if not GEMINI_WIKI.exists():
        append_log(f"ERROR: GEMINI_WIKI 부재 - {GEMINI_WIKI}")
        return False
    log_path = WIKI_LOG / f"gemini_wiki_{datetime.now().strftime('%Y%m%d')}.log"
    # 챕터 모드 auto: 긴 책(30만자↑)+진짜 장구조면 장별 노트, 아니면 단일 노트로 자동 폴백.
    if CHAPTER_WIKI.exists():
        cmd = [cfg.PYTHON, "-u", str(CHAPTER_WIKI), "--file", str(txt_path), "--mode", "auto"]
    else:
        cmd = [cfg.PYTHON, "-u", str(GEMINI_WIKI), "--file", str(txt_path)]
    try:
        env = _wiki_env(wiki_target)
        subprocess.Popen(cmd, stdout=open(log_path, "a", encoding="utf-8"),
                         stderr=subprocess.STDOUT, env=env)
        append_log(f"Wiki 트리거({'챕터auto' if CHAPTER_WIKI.exists() else 'gemini'}): {Path(txt_path).name}"
                   + (f" → 보관함(Vault) {env['MYBOOKSHELF_WIKI_DIR']}" if "MYBOOKSHELF_WIKI_DIR" in env else ""))
        return True
    except Exception as e:
        append_log(f"ERROR: gemini_wiki Popen 실패 ({type(e).__name__}) {str(e)[:200]}")
        return False


def check_wiki_orphans() -> dict:
    """raw → wiki → processed 3단계 누락 자리 감지 (2026-05-16 신설).
    raw/processed 이동 버그(2026-05-14 관측) 흔적 자동 감지용.

    반환:
      - wiki_done_raw_remaining: wiki 본문(.md)은 생성됐는데 raw .txt가 남아 있는 자리
        (wiki_generator.py가 raw → processed 이동에 실패한 흔적)
      - raw_pending: 아직 처리되지 않은 raw .txt 개수
      - wiki_total: 생성된 wiki .md 총 개수
    """
    wiki_stems = {p.stem for p in WIKI_DIR.rglob("*.md")}
    raw_files = [f for f in RAW_DIR.rglob("*.txt")
                 if not (PROCESSED_DIR / f.name).exists()]
    # wiki는 됐는데 raw가 남아 있는 자리
    orphans = [f for f in raw_files if f.stem in wiki_stems]
    pending = [f for f in raw_files if f.stem not in wiki_stems]
    return {
        "wiki_done_raw_remaining": len(orphans),
        "orphan_files": [str(f) for f in orphans[:10]],  # 표시용 상위 10건
        "raw_pending": len(pending),
        "wiki_total": len(wiki_stems),
    }


# ─── Obsidian 보관함(Vault) 관리 ─────────────────────────────

def _obsidian_config() -> Path:
    # 맥은 APPDATA가 없음 — Application Support 경로 사용 (2026-07-03)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    return Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json"


def ensure_obsidian_vault(folder: Path) -> bool:
    """folder를 옵시디언 보관함(Vault) 목록에 등록(이미 있으면 그대로). (2026-06-11)"""
    cfgf = _obsidian_config()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        data = json.loads(cfgf.read_text(encoding="utf-8")) if cfgf.exists() else {}
        vaults = data.setdefault("vaults", {})
        for v in vaults.values():
            try:
                if Path(v.get("path", "")).resolve() == folder.resolve():
                    return True
            except Exception:
                continue
        import secrets
        vaults[secrets.token_hex(8)] = {"path": str(folder.resolve()),
                                        "ts": int(datetime.now().timestamp() * 1000)}
        cfgf.parent.mkdir(parents=True, exist_ok=True)
        cfgf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        append_log(f"WARN: 옵시디언 보관함(Vault) 등록 실패 ({type(e).__name__}) {str(e)[:120]}")
        return False


def list_obsidian_vaults() -> list[str]:
    """옵시디언에 등록된 보관함(Vault) 경로 목록. (2026-06-11)"""
    try:
        data = json.loads(_obsidian_config().read_text(encoding="utf-8"))
        return [v.get("path", "") for v in data.get("vaults", {}).values() if v.get("path")]
    except Exception:
        return []


def set_wiki_dir(path_str: str) -> None:
    """~/.config/mybookshelf/config.json의 dirs.wiki 갱신 — 앱 재시작 후 적용. (2026-06-11)"""
    f = cfg.CONFIG_FILE
    try:
        d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        d = {}
    d.setdefault("dirs", {})["wiki"] = path_str
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def open_wiki_vault():
    """위키 폴더를 옵시디언 보관함(Vault)로 등록 후 옵시디언으로 열기. 실패 시 폴더라도 연다."""
    ensure_obsidian_vault(WIKI_DIR)
    from urllib.parse import quote
    uri = "obsidian://open?path=" + quote(str(WIKI_DIR.resolve()))
    try:
        if sys.platform == "darwin":   # os.startfile은 윈도우 전용 (2026-07-03)
            subprocess.run(["open", uri])
        else:
            os.startfile(uri)
    except Exception:
        open_path(WIKI_DIR)


# ─── 챕터 요약 → Obsidian 노트 ───────────────────────────────

def _ch_link(stem: str, ch_title: str) -> str:
    """챕터 노트의 Obsidian 위키링크 문자열 반환."""
    try:
        import gemini_wiki as _gw
        return "[[" + _gw.make_filename(_gw.nfc(f"{stem} — {ch_title}"))[:-3] + "]]"
    except Exception:
        return f"[[{stem} — {ch_title}]]"


def build_single_chapter_wiki(ws_name: str, stem: str, summary_path: Path, wiki_dir: Path | None = None) -> tuple[bool, str]:
    """단일 챕터 요약(_wiki.md, 구형 _wiki.json) → 개별 Obsidian 노트. (ok, path or msg)."""
    try:
        import gemini_wiki as _gw
    except ImportError as e:
        return False, f"임포트 실패: {e}"
    d = load_summary_file(summary_path)
    if d is None:
        return False, f"요약 파일 읽기 실패: {summary_path.name}"
    json_path = summary_path
    ch_title = _re.sub(r"^\d+_", "", json_path.stem.replace("_wiki", ""))
    body  = d.get("body", "")
    summ  = d.get("summary", "")
    today = __import__("datetime").date.today().isoformat()
    _, model = llm.wiki_provider_model()

    # 이전/다음 챕터 탐색 (chapters_dir 내 ??_*.txt 순서 기준)
    ch_dir = json_path.parent
    all_stems = [_re.sub(r"^\d+_", "", f.stem)
                 for f in sorted(ch_dir.glob("??_*.txt"))
                 if not f.stem.endswith(("_ko", "_wiki"))]
    cur_idx = all_stems.index(ch_title) if ch_title in all_stems else -1
    prev_link = _ch_link(stem, all_stems[cur_idx - 1]) if cur_idx > 0 else ""
    next_link = _ch_link(stem, all_stems[cur_idx + 1]) if 0 <= cur_idx < len(all_stems) - 1 else ""
    nav = " · ".join(x for x in [prev_link, next_link] if x)

    book_link = "[[" + _gw.make_filename(_gw.nfc(stem))[:-3] + "]]"
    note_title = f"{stem} — {ch_title}"

    lines = [
        "---", f"title: {note_title}", f"book: {stem}",
        f"chapter: {ch_title}", f"model: {model}",
        f"generated: {today}", "---", "",
        f"# {ch_title}",
        f"> ← {book_link}" + (f"  |  {nav}" if nav else ""), "",
        f"**요약:** {summ}", "", body, "",
        "---",
        f"*전체 목차: {book_link}*" + (f"  |  {nav}" if nav else ""),
    ]
    fname = _gw.make_filename(_gw.nfc(note_title))
    _wdir = wiki_dir or WIKI_DIR
    # 챕터 노트는 책 이름 하위폴더에 저장
    book_folder = _wdir / _re.sub(r'[/\\:*?"<>|]', '_', stem).strip()
    book_folder.mkdir(parents=True, exist_ok=True)
    out_path = book_folder / fname
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return True, str(out_path)


def build_wiki_from_chapter_summaries(ws_name: str, stem: str, wiki_dir: Path | None = None) -> tuple[bool, str]:
    """챕터 요약(_wiki.md, 구형 _wiki.json)들 → 옵시디언 위키 노트 생성. (ok, path or msg)."""
    try:
        import chapter_wiki as _cw
        import gemini_wiki as _gw
    except ImportError as e:
        return False, f"임포트 실패: {e}"
    ch_dir = chapters_dir(ws_name, stem)
    if not ch_dir.exists():
        return False, "챕터 폴더 없음 — 1단계를 먼저 실행하세요"
    json_files = list_summary_files(ch_dir)
    # 기존 wiki 파일 자동 정리 (잘못된 이름/이전 생성물 제거 후 재생성)
    _wdir_pre = wiki_dir or WIKI_DIR
    _safe_stem = _re.sub(r'[/\\:*?"<>|]', '_', stem).strip()
    _book_folder_pre = _wdir_pre / _safe_stem
    if _book_folder_pre.exists():
        import shutil as _shutil
        _shutil.rmtree(str(_book_folder_pre), ignore_errors=True)
    _hub_pre = _wdir_pre / (_gw.make_filename(_gw.nfc(stem)) if hasattr(_gw, "make_filename") else f"{_safe_stem}.md")
    if _hub_pre.exists():
        _hub_pre.unlink()
    if not json_files:
        return False, "요약 파일 없음 — 3단계를 먼저 실행하세요"
    sections = []
    for i, jf in enumerate(json_files, 1):
        d = load_summary_file(jf)
        if d is None:
            continue
        title = _re.sub(r"^\d+_", "", jf.stem.replace("_wiki", ""))
        sections.append({"idx": i, "title": title,
                         "summary": d.get("summary", ""),
                         "body": d.get("body", "")})
    if not sections:
        return False, "유효한 요약 없음"
    # 책 전체요약: _overview.md가 있으면 그대로 사용 (요약 단계에서 생성·수정한
    # 것을 존중 — 2026-07-07). 없으면 즉석 생성 후 편집 가능하게 저장해 둔다.
    ov_file = find_overview_file(ws_name, stem)
    ov = load_overview_file(ov_file) if ov_file else None
    if ov is None:
        ok_ov, _msg_ov = summarize_book_overview(ws_name, stem)
        ov_file = find_overview_file(ws_name, stem) if ok_ov else None
        ov = load_overview_file(ov_file) if ov_file else None
    if ov is None:
        ov = _cw.generate_overview(stem, sections)
    cat  = ov.get("category", "기타")
    intro = ov.get("intro", "")
    summ  = ov.get("summary", "")
    author = (ov.get("author") or "").strip() or _author_from_stem(stem)
    today = __import__("datetime").date.today().isoformat()
    # 전체요약을 만든 실제 모델을 이어받는다 (LLM 무호출 반영 시 'default' 방지)
    model = (ov.get("model") or "").strip() or llm.effective_wiki_model()
    _wdir = wiki_dir or WIKI_DIR
    # 챕터 노트는 책 이름 하위폴더에 저장
    _book_folder = _wdir / _re.sub(r'[/\\:*?"<>|]', '_', stem).strip()

    # 챕터별 개별 노트 존재 여부 확인 (하위폴더 우선, 루트 폴백)
    all_ch_titles = [s["title"] for s in sections]
    def _has_ch_note(title):
        fname = _gw.make_filename(_gw.nfc(f"{stem} — {title}"))
        return (_book_folder / fname).exists() or (_wdir / fname).exists()

    tags_str = json.dumps([cat], ensure_ascii=False) if cat and cat != "기타" else "[]"
    lines = [
        "---", f"title: {stem}", f"category: {cat}",
        f"tags: {tags_str}",
        f"source: {stem}.txt",
    ]
    if author:
        lines.append(f"author: {author}")
    lines += [
        f"model: {model}", f"generated: {today}", "---", "",
        f"# {stem}", "", intro, "", f"**요약:** {summ}", "",
    ]

    # 챕터 목차 (개별 노트 있으면 ✅, 없으면 📄)
    toc_lines = ["## 📋 챕터 목차", ""]
    for s in sections:
        exists = _has_ch_note(s["title"])
        marker = "✅" if exists else "📄"
        ch_note_link = _ch_link(stem, s["title"])
        toc_lines.append(f"- {marker} {ch_note_link} — {s['summary'][:60]}{'…' if len(s['summary'])>60 else ''}")
    lines += toc_lines + [""]

    # 챕터 섹션: 개별 노트 있으면 링크만, 없으면 요약+본문 인라인
    for s in sections:
        exists = _has_ch_note(s["title"])
        ch_note_link = _ch_link(stem, s["title"])
        if exists:
            # 개별 노트 이미 있음 → 링크와 한 줄 요약만 (중복 방지)
            lines += [
                f"## {s['idx']:02d}. {s['title']}",
                f"> 📄 {ch_note_link}",
                "",
                s["summary"], "",
            ]
        else:
            # 개별 노트 없음 → 요약+본문 인라인 포함
            lines += [
                f"## {s['idx']:02d}. {s['title']}",
                f"> {ch_note_link}",
                "",
                s["summary"], "",
                s["body"], "",
            ]

    # 기존 개별 챕터 노트들의 책 링크가 올바른지 확인 후 업데이트 (하위폴더 우선)
    book_link = "[[" + _gw.make_filename(_gw.nfc(stem))[:-3] + "]]"
    for i, s in enumerate(sections):
        fname = _gw.make_filename(_gw.nfc(f"{stem} — {s['title']}"))
        note_path = (_book_folder / fname) if (_book_folder / fname).exists() else (_wdir / fname)
        if not note_path.exists():
            continue
        content = note_path.read_text(encoding="utf-8")
        # 책 링크가 없으면 상단에 추가
        if book_link not in content:
            prev_link = _ch_link(stem, all_ch_titles[i-1]) if i > 0 else ""
            next_link = _ch_link(stem, all_ch_titles[i+1]) if i < len(all_ch_titles)-1 else ""
            nav = " · ".join(x for x in [prev_link, next_link] if x)
            new_head = f"> ← {book_link}" + (f"  |  {nav}" if nav else "")
            # 첫 번째 # 제목 다음 줄에 삽입
            updated = _re.sub(
                r"(^# .+\n)", rf"\1{new_head}\n", content, count=1, flags=_re.MULTILINE
            )
            note_path.write_text(updated, encoding="utf-8")

    # 전체 요약 노트는 보관함(Vault) 루트에 저장 (폴더 브라우징 시 책 목록 한눈에)
    _wdir.mkdir(parents=True, exist_ok=True)
    out_path = _wdir / _gw.make_filename(_gw.nfc(stem))
    out_path.write_text("\n".join(lines), encoding="utf-8")
    _gw.mark_done(_gw.nfc(stem + ".txt"))
    append_log(f"단계별 Wiki 생성 완료: {out_path.name}")
    return True, str(out_path)
