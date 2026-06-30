#!/usr/bin/env python3
"""My Bookshelf — PDF→Wiki 파이프라인 (Streamlit GUI)"""

import json
import os
from difflib import SequenceMatcher
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import config as cfg
import llm_providers as llm
from version import APP_VERSION

# ── 설정 ─────────────────────────────────────────────────
# 기계 의존 값(경로·바이너리·분류 폴더)은 전부 config.py가 해석한다.
# 기본값 ~/Documents/My Bookshelf, 덮어쓰기 ~/.config/mybookshelf/config.json.
WORKSPACES = cfg.WORKSPACES   # 보관 폴더 이름 목록. 첫 항목이 기본값.

UPLOAD_TMP    = cfg.UPLOAD_TMP
RAW_DIR       = cfg.RAW_DIR
WIKI_DIR      = cfg.WIKI_DIR
PROCESSED_DIR = cfg.PROCESSED_DIR
DONE_DIR      = cfg.DONE_DIR
OLD_DONE_DIR  = cfg.OLD_DONE_DIR            # 옛 fallback (사용 안 함, 호환용)
FAILED_DIR    = cfg.FAILED_DIR
# translated/는 done/<ws>/_translated/로 통합 (2026-05-18).
# OLD_TRANSLATED_DIR은 데이터 이동 이전 옛 위치 — fallback 용도로만 유지.
OLD_TRANSLATED_DIR = cfg.OLD_TRANSLATED_DIR
# done/<ws>/ 하위 산출물 폴더명 — 텍스트 처리 순서대로 번호 접두 (2026-06-09).
#   1_txt(②변환 TXT, Gemini 입력) → 2_md(③MD, 장 구조) → 3_translated(④번역)
TXT_SUB   = "1_txt"
MD_SUB    = "2_md"
TRANS_SUB = "3_translated"
PDF_SUB   = "pdf"          # 원본 PDF 보관 폴더
LOG_FILE      = cfg.LOG_FILE
RESULTS_FILE  = cfg.RESULTS_FILE

for _d in [DONE_DIR, FAILED_DIR, RAW_DIR, WIKI_DIR, PROCESSED_DIR, UPLOAD_TMP,
           LOG_FILE.parent, RESULTS_FILE.parent]:
    _d.mkdir(parents=True, exist_ok=True)

CATEGORY_ICONS: dict[str, str] = {}  # 워크스페이스 이름 → 이모지. 빈 경우 기본 📚 사용

GEMINI_WIKI    = cfg.find_script("gemini_wiki.py")    # 2026-06-09 위키=Gemini로 교체
CHAPTER_WIKI   = cfg.find_script("chapter_wiki.py")   # 2026-06-09 챕터 모드(긴 책 자동 장별)
WIKI_LOG       = cfg.WIKI_LOG_DIR


# ── 폴더 구조 헬퍼 ────────────────────────────────────────
# done/<ws>/<file>.pdf      ← PDF는 워크스페이스 루트
# done/<ws>/_txt/<file>.txt ← MD 성공 시 TXT는 _txt/
# done/<ws>/_md/<file>.md   ← MD는 _md/ (분할본도 동일)
# MD 생성 실패 시 TXT는 루트에 남아 미완료 신호로 사용

import re as _re

def txt_dir(base: Path, ws_name: str) -> Path:
    return base / ws_name / TXT_SUB

def md_dir(base: Path, ws_name: str) -> Path:
    return base / ws_name / MD_SUB

def translated_dir(base: Path, ws_name: str) -> Path:
    """bilingual.txt를 두는 폴더. done/<ws>/_translated/. (2026-05-18 통합)"""
    return base / ws_name / TRANS_SUB

def _nfc(s: str) -> str:
    """맥 파일명은 NFD라 비교 전 NFC 정규화 필수 (한글)."""
    return unicodedata.normalize("NFC", s)


_PROC_STEMS_CACHE: dict = {"t": 0.0, "stems": set()}


def processed_stems(max_age: float = 60.0) -> set[str]:
    """이미 처리된 파일의 NFC stem 집합 — done 폴더 산출물 + 위키 완료 기록.
    업로드 중복 건너뛰기용. 대량 배치 중 파일마다 rglob하지 않게 60초 캐시. (v0.3.2)"""
    now = time.time()
    if now - _PROC_STEMS_CACHE["t"] < max_age and _PROC_STEMS_CACHE["stems"]:
        return _PROC_STEMS_CACHE["stems"]
    stems: set[str] = set()
    try:
        if DONE_DIR.exists():
            for p in DONE_DIR.rglob("*"):
                if p.is_file() and p.suffix.lower() in {".pdf", ".txt", ".md", ".docx", ".doc"}:
                    stems.add(_nfc(p.stem))
        gd = cfg.GEMINI_DONE_FILE
        if gd.exists():
            for line in gd.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line:
                    stems.add(_nfc(Path(line).stem))
    except Exception as e:
        append_log(f"WARN: processed_stems 수집 실패 ({type(e).__name__}) {str(e)[:120]}")
    _PROC_STEMS_CACHE["t"] = now
    _PROC_STEMS_CACHE["stems"] = stems
    return stems


def _bilingual_candidates(stem: str, exclude_ws: str | None = None) -> list[Path]:
    """모든 워크스페이스에서 같은 stem의 bilingual.txt 후보 경로 수집. (2026-05-18 cross-ws resume)"""
    paths: list[Path] = []
    if DONE_DIR.exists():
        for ws_dir in DONE_DIR.iterdir():
            if not ws_dir.is_dir() or ws_dir.name == exclude_ws:
                continue
            bil = translated_dir(DONE_DIR, ws_dir.name) / f"{stem}_bilingual.txt"
            if bil.exists():
                paths.append(bil)
    if OLD_TRANSLATED_DIR.exists():
        for ws_dir in OLD_TRANSLATED_DIR.iterdir():
            if not ws_dir.is_dir() or ws_dir.name == exclude_ws:
                continue
            bil = ws_dir / f"{stem}_bilingual.txt"
            if bil.exists():
                paths.append(bil)
    return paths


def _parse_bilingual_block(block: str) -> tuple[str, str] | None:
    """[EN]/[KO] 구형 또는 태그 없는 교차 신형 블록을 (원문, 번역) 으로 파싱."""
    block = block.strip()
    if not block:
        return None
    if "\n\n[KO]\n" in block:                          # 구형: [EN]\n...\n\n[KO]\n...
        en_part, tgt = block.split("\n\n[KO]\n", 1)
        src = en_part[len("[EN]\n"):].strip() if en_part.startswith("[EN]\n") else en_part.strip()
        return src, tgt.strip()
    if not block.startswith("[") and "\n\n" in block:  # 신형: 원문\n\n번역
        src, tgt = block.split("\n\n", 1)
        return src.strip(), tgt.strip()
    if block.startswith("[EN]\n"):                      # 미번역 구형 단독 블록
        return block[len("[EN]\n"):].strip(), ""
    return None


def _ko_block_count(p: Path) -> int:
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "\n\n[KO]\n" in text:
            return text.count("\n\n[KO]\n")            # 구형
        blocks = [b.strip() for b in text.split("\n\n---\n\n") if b.strip()]
        return sum(1 for b in blocks if "\n\n" in b and not b.startswith("["))  # 신형
    except Exception:
        return 0


def find_cross_ws_bilingual(stem: str, exclude_ws: str) -> Path | None:
    """다른 ws에서 같은 stem bilingual.txt 후보 중 [KO] 블록이 가장 많은 파일 반환."""
    cands = _bilingual_candidates(stem, exclude_ws=exclude_ws)
    if not cands:
        return None
    cands.sort(key=_ko_block_count, reverse=True)
    top = cands[0]
    return top if _ko_block_count(top) > 0 else None


def collect_cross_ws_cache(stem: str, exclude_ws: str) -> dict:
    """다른 모든 ws의 bilingual.txt에서 원문→번역 매핑 합쳐 dict 반환. 보존마커 제외."""
    cache: dict = {}
    for p in _bilingual_candidates(stem, exclude_ws=exclude_ws):
        try:
            for block in p.read_text(encoding="utf-8", errors="ignore").split("\n\n---\n\n"):
                parsed = _parse_bilingual_block(block)
                if not parsed:
                    continue
                src, tgt = parsed
                if not src or not tgt or tgt.startswith("(원문 보존"):
                    continue
                cache.setdefault(src, tgt)
        except Exception:
            continue
    return cache


def find_bilingual(ws_name: str, stem: str) -> Path | None:
    """bilingual.txt 우선 검색 — 새 위치(done/<ws>/_translated/) 먼저, 옛 위치(translated/<ws>/) fallback."""
    new = translated_dir(DONE_DIR, ws_name) / f"{stem}_bilingual.txt"
    if new.exists():
        return new
    old = OLD_TRANSLATED_DIR / ws_name / f"{stem}_bilingual.txt"
    if old.exists():
        return old
    return None

def find_txt(base: Path, ws_name: str, stem: str) -> Path | None:
    """_txt/ 우선, 없으면 워크스페이스 루트에서 .txt 찾기."""
    p1 = txt_dir(base, ws_name) / f"{stem}.txt"
    if p1.exists(): return p1
    p2 = base / ws_name / f"{stem}.txt"
    return p2 if p2.exists() else None

def find_md(base: Path, ws_name: str, stem: str) -> Path | None:
    """_md/ 우선, 없으면 워크스페이스 루트에서 .md 찾기."""
    p1 = md_dir(base, ws_name) / f"{stem}.md"
    if p1.exists(): return p1
    p2 = base / ws_name / f"{stem}.md"
    return p2 if p2.exists() else None

def find_pdf(base: Path, ws_name: str, name: str) -> Path | None:
    """워크스페이스 루트에서 PDF 찾기."""
    p = base / ws_name / name
    return p if p.exists() else None

def find_split_mds(base: Path, ws_name: str, stem: str) -> list[Path]:
    """<stem>_NN_*.md 분할본."""
    pat = _re.compile(rf"^{_re.escape(stem)}_\d{{2}}_.+\.md$")
    out: list[Path] = []
    for d in (md_dir(base, ws_name), base / ws_name):
        if d.exists():
            out.extend(p for p in d.iterdir() if p.is_file() and pat.match(p.name))
    return out


# ── 파이프라인 함수들 ─────────────────────────────────────

def pdf_to_txt(pdf_path: Path, fast: bool = True) -> tuple[Path | None, Path | None, str]:
    """텍스트 레이어가 있는 PDF를 TXT로 변환한다."""
    pdftotext = cfg.PDFTOTEXT

    txt_path = Path(tempfile.gettempdir()) / (pdf_path.stem + ".txt")

    # Windows에서 터미널 창이 뜨지 않도록 STARTUPINFO + CREATE_NO_WINDOW 조합 사용
    if sys.platform == "win32":
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0  # SW_HIDE
        _nw = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": _si,
               "stdin": subprocess.DEVNULL}
    else:
        _nw = {}

    if not pdftotext or not Path(pdftotext).exists():
        return None, None, "TXT 변환에 필요한 pdftotext가 없습니다 (macOS: brew install poppler)"

    r = subprocess.run([pdftotext, "-layout", str(pdf_path), str(txt_path)],
                       capture_output=True, text=True, **_nw)
    if r.returncode != 0:
        return None, None, f"pdftotext 오류 (exit {r.returncode}): {(r.stderr or '').strip() or '알 수 없는 오류'}"

    if not txt_path.exists() or txt_path.stat().st_size == 0:
        return None, None, "텍스트 추출 실패 — 텍스트 레이어가 있는 PDF만 변환할 수 있습니다"

    return txt_path, None, ""


# ── 번역: 영어→한국어 고정 ────────────────────────────────
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
    if llm.claude_cli_available():
        for m, lbl in (("claude-sonnet-4-6", "Claude Sonnet 4.6"),
                       ("claude-haiku-4-5", "Claude Haiku 4.5")):
            opts.append((f"claude_cli:{m}", f"{lbl} (구독·CLI)", True, "구독 로그인"))
    for prov, info in llm.PROVIDERS.items():
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
    import re as _re
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


def wiki_generator_running() -> bool:
    if sys.platform == "darwin":
        r = subprocess.run(["pgrep", "-f", "gemini_wiki.py"], capture_output=True)
        return r.returncode == 0
    # 윈도우: pgrep 없음 — psutil로 커맨드라인 검사 (2026-06-11 윈도우 크래시 수정)
    try:
        import psutil
        return any(
            "gemini_wiki.py" in " ".join(p.info.get("cmdline") or [])
            for p in psutil.process_iter(["cmdline"])
        )
    except Exception:
        return False


def _wiki_env() -> dict:
    """위키 생성기 자식 프로세스 환경. 업로드 탭에서 고른 보관함(Vault)가 있으면
    MYBOOKSHELF_WIKI_DIR로 전달(config.py가 WIKI_DIR로 해석). (2026-06-11)"""
    env = {**os.environ, "PYTHONUTF8": "1"}   # 윈도우 cp949에서 이모지 출력 크래시 방지
    target = (st.session_state.get("wiki_target_dir") or "").strip()
    if target and Path(target).expanduser().resolve() != WIKI_DIR.resolve():
        env["MYBOOKSHELF_WIKI_DIR"] = target
    return env


def trigger_wiki_generation() -> int:
    """미처리 책을 Gemini 위키 생성기로 일괄 생성(--all). (2026-06-09 Gemini화)
    add_pdf/raw/processed의 *.txt 중 gemini_done에 없는 것을 처리한다."""
    if wiki_generator_running():
        return 0
    if not GEMINI_WIKI.exists():
        append_log(f"ERROR: GEMINI_WIKI 부재 - {GEMINI_WIKI}")
        return 0
    log_path = WIKI_LOG / f"gemini_wiki_{datetime.now().strftime('%Y%m%d')}.log"
    try:
        env = _wiki_env()
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


def trigger_gemini_wiki(txt_path: Path) -> bool:
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
        env = _wiki_env()
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


def append_log(msg: str):   # encoding 미지정이면 윈도우 cp949 → 이모지에서 크래시 (2026-06-11)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{ts}] {msg}\n")


def _save_bilingual_atomic(path: Path, blocks: list[str]):
    """tmp 경유 원자적 저장 — 단락마다 호출해도 파일이 깨지지 않음.

    덮어쓰기 가드 (2026-05-17 추가, 2602.21012 손실 사고 재발 방지):
    기존 파일의 블록 수가 새 블록 수보다 *크면* 진행분 손실 위험으로 판단,
    `.bakN` 회전 후 저장. N은 1부터 시작, 기존 .bakN 존재 시 N+1.
    """
    new_n = len(blocks)
    if path.exists() and new_n >= 0:
        try:
            existing = path.read_text(encoding="utf-8", errors="ignore")
            existing_n = sum(
                1 for b in existing.split("\n\n---\n\n") if b.strip()
            )
        except Exception:
            existing_n = 0
        if new_n < existing_n:
            i = 1
            while True:
                bak = path.with_name(path.name + f".bak{i}")
                if not bak.exists():
                    break
                i += 1
            try:
                path.rename(bak)
                append_log(
                    f"GUARD: 덮어쓰기 차단 — 기존 {existing_n}블록 > "
                    f"새 {new_n}블록 ({path.name}), 백업 회전 → {bak.name}"
                )
            except Exception as e:
                append_log(
                    f"GUARD: 백업 회전 실패 ({type(e).__name__}): {e} — "
                    f"저장은 진행"
                )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n\n---\n\n".join(blocks), encoding="utf-8")
    tmp.replace(path)


def _save_en_ko_split(bilingual_path: Path, blocks: list[str]):
    """bilingual blocks에서 영어 원본·한글 본만 분리해 _en.txt·_ko.txt로 저장 (2026-05-19)."""
    stem = bilingual_path.stem.removesuffix("_bilingual")
    en_path = bilingual_path.parent / f"{stem}_en.txt"
    ko_path = bilingual_path.parent / f"{stem}_ko.txt"
    en_lines = []
    ko_lines = []
    for b in blocks:
        b = b.strip()
        if not b: continue
        parsed = _parse_bilingual_block(b)
        if parsed:
            src_text, tgt_text = parsed
            if src_text:
                en_lines.append(src_text)
            if tgt_text and not tgt_text.startswith("(원문 보존"):
                ko_lines.append(tgt_text)
    try:
        en_path.write_text("\n\n".join(en_lines), encoding="utf-8")
        ko_path.write_text("\n\n".join(ko_lines), encoding="utf-8")
    except Exception:
        pass


# ─── 재시도 대기 파일 wrapper (file_uploader 인터페이스 모방, 2026-05-19) ──
class _PathAsUpload:
    """Path를 file_uploader 결과와 같은 인터페이스로 감싸기."""
    def __init__(self, p):
        self._p = Path(p)
        self.name = self._p.name
    def read(self) -> bytes:
        return self._p.read_bytes()
    def seek(self, pos: int):
        pass   # read()가 매번 디스크에서 새로 읽음 — UploadedFile.seek 호환용 (2026-06-11)


# ─── 일시정지 플래그 (워커 thread ↔ 메인 UI 통신, 2026-05-19) ──────────
PAUSE_DIR = cfg.PAUSE_DIR
PAUSE_DIR.mkdir(parents=True, exist_ok=True)


def pause_flag_path(stem: str) -> Path:
    """파일명 안전화 — 한글·공백 그대로 둠 (Path가 처리)."""
    return PAUSE_DIR / f"{stem}.pause"


def is_paused(stem: str) -> bool:
    return pause_flag_path(stem).exists()


def set_paused(stem: str, paused: bool):
    p = pause_flag_path(stem)
    if paused:
        p.touch()
    else:
        if p.exists():
            try: p.unlink()
            except Exception: pass


# ─── 한 파일 통째 처리 함수 (Phase 1 + Phase 2, 2026-05-19 추출) ────
def _process_file_for_pipeline(uf, ws_name, ws_slug, do_translate, translate_engine,
                                force_reembed, defer_embed, placeholder, do_wiki=True):
    """한 파일 Phase 1+2 통째 처리. UI는 placeholder.container() 안에서.
    result dict 반환. 워커 스레드에서도 안전 (placeholder 격리)."""
    with placeholder.container():
        return _process_file_inner(uf, ws_name, ws_slug, do_translate, translate_engine,
                                    force_reembed, defer_embed, do_wiki=do_wiki)

def _process_file_inner(uf, ws_name, ws_slug, do_translate, translate_engine,
                         force_reembed, defer_embed, do_wiki=True):
    """실제 처리 본문."""
    st.subheader(f"📄 {uf.name}")

    # ── 이미 처리된 파일 건너뛰기 (2026-06-11 v0.3.2) ──
    # done 폴더 산출물·위키 완료 기록과 stem(NFC) 대조. 토글 끄면 강제 재처리.
    if st.session_state.get("skip_processed_flag", True) \
            and _nfc(Path(uf.name).stem) in processed_stems():
        st.info("⏭️ **이미 처리된 파일** — 건너뜁니다. 재처리하려면 '이미 처리된 파일 건너뛰기' 토글을 끄세요.")
        append_log(f"건너뜀(이미 처리됨): {uf.name}")
        _src = getattr(uf, "_p", None)
        if _src is not None:                       # 재시도 대기열이면 큐에서 제거
            try:
                Path(_src).unlink()
            except Exception:
                pass
        _stages = {"ocr": "skip", "txt": "skip", "md": "skip",
                   "bilingual": "skip", "anythingllm": "skip", "wiki": "skip"}
        return {"name": uf.name, "ok": True, "ws": ws_name, "stages": _stages,
                "pdf_path": "", "txt_path": "", "md_path": "", "bilingual_path": "",
                "skipped": True}

    dest = UPLOAD_TMP / uf.name
    # 재시도 파일은 이미 UPLOAD_TMP에 있음 — 자기 자신에 덮어쓰면 open("wb")가
    # 먼저 비워서 0바이트로 잘린다. 같은 파일이면 복사 생략. (2026-06-11)
    _src = getattr(uf, "_p", None)
    if not (_src is not None and Path(_src).resolve() == dest.resolve()):
        uf.seek(0)   # 같은 업로드로 재실행 시 포인터가 끝에 있어 0바이트 저장되는 것 방지
        with open(dest, "wb") as f:
            f.write(uf.read())

    success     = True
    txt_path    = None
    md_src      = None
    upload_file = None
    final_pdf = final_txt = final_md = None
    partial_fail_n = 0   # 번역 부분 실패 단락 수 (>0 이면 failed 미이동 + 큐 보류)

    with st.status(f"변환/번역 중: {uf.name}", expanded=True) as status_ui:
        # Phase 1 inline — 기존 코드 그대로
        if dest.suffix.lower() == ".pdf":
            st.write("🔄 **1단계** · PDF → TXT 변환")
            txt_path, md_src, conv_err = pdf_to_txt(dest)
            if txt_path:
                st.write(f"✅ TXT 변환 완료 → `{txt_path.name}`")
                append_log(f"PDF→TXT 변환 완료: {txt_path.name}")
                if md_src:
                    st.write(f"✅ MD 사이드카 생성 → `{md_src.name}` ({md_src.stat().st_size // 1024} KB)")
                else:
                    st.write("⚠️ MD 사이드카 생성 실패 (비치명적)")
            else:
                st.write(f"❌ TXT 변환 실패 — {conv_err}")
                st.error(f"**변환 실패 원인:** {conv_err}")
                append_log(f"ERROR: TXT 변환 실패 - {uf.name} ({conv_err})")
                shutil.move(str(dest), str(FAILED_DIR / uf.name))
                status_ui.update(label=f"❌ 실패: {uf.name}", state="error")
                success = False
        else:
            txt_path = dest
            st.write(f"ℹ️ **1단계** · PDF 아님 — 원본 그대로 사용 (`{dest.name}`)")

        upload_file = txt_path
        _is_en = (txt_path is not None and txt_path.exists() and needs_translation(txt_path))
        will_translate = do_translate and success and _is_en
        if do_translate and success and txt_path and txt_path.exists():
            _tgt_name = "한국어"
            st.caption(f"🔍 언어 감지: {f'외국어 → {_tgt_name} 번역 진행' if _is_en else f'이미 {_tgt_name} → 번역 스킵'}")

        if will_translate:
            text_raw = txt_path.read_text(encoding="utf-8", errors="ignore")
            paragraphs = _split_paragraphs_robust(text_raw)
            if len(paragraphs) < 5:
                st.warning(f"⚠️ 단락 분할 결과가 {len(paragraphs)}개에 그쳤습니다 (원본 {len(text_raw)}자).")
                append_log(f"WARN: 단락 분할 부족 — {uf.name} paragraphs={len(paragraphs)}")
            bilingual_path = translated_dir(DONE_DIR, ws_name) / (txt_path.stem + "_bilingual.txt")
            translated_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
            _legacy = RAW_DIR / ws_name / (txt_path.stem + "_bilingual.txt")
            if _legacy.exists() and not bilingual_path.exists():
                shutil.move(str(_legacy), str(bilingual_path))
            _legacy_old_translated = OLD_TRANSLATED_DIR / ws_name / (txt_path.stem + "_bilingual.txt")
            if _legacy_old_translated.exists() and not bilingual_path.exists():
                shutil.move(str(_legacy_old_translated), str(bilingual_path))
            if not bilingual_path.exists():
                _cross_src = find_cross_ws_bilingual(txt_path.stem, ws_name)
                if _cross_src is not None:
                    shutil.copy2(str(_cross_src), str(bilingual_path))
                    _src_ws = _cross_src.parent.parent.name
                    _src_ko = _ko_block_count(_cross_src)
                    append_log(f"♻️ cross-ws resume: {txt_path.stem} ({_src_ws} → {ws_name}, KO {_src_ko}건)")
                    st.info(f"♻️ 다른 워크스페이스 진행분을 발견해 이어받았습니다 (`{_src_ws}` → `{ws_name}`, [KO] {_src_ko}건)")

            cached: dict = {}
            if bilingual_path.exists():
                for block in bilingual_path.read_text(encoding="utf-8", errors="ignore").split("\n\n---\n\n"):
                    block = block.strip()
                    parsed = _parse_bilingual_block(block)
                    if not parsed or not parsed[1]: continue
                    cached[parsed[0]] = parsed[1]
            _cross_cache = collect_cross_ws_cache(txt_path.stem, ws_name)
            if _cross_cache:
                _before = len(cached)
                for _en, _ko in _cross_cache.items():
                    cached.setdefault(_en, _ko)
                _added = len(cached) - _before
                if _added > 0:
                    append_log(f"♻️ cross-ws 캐시 합침: {txt_path.stem} +{_added}건")
                    st.caption(f"♻️ 다른 워크스페이스 캐시 {_added}건 추가 합침")

            # 고유명사 용어집 — 단락이 진행되며 누적, 이후 단락엔 한글만 쓰게 전달 (2026-06-11)
            _name_glossary: dict[str, str] = {}
            _tr_fn = lambda p, _e=translate_engine, _g=_name_glossary: translate(p, _e, _g)
            _tr_label = engine_label(translate_engine)
            skip_section_idxs   = find_skip_section_paragraphs(paragraphs)
            skip_individual_idxs = {i for i, p in enumerate(paragraphs) if should_skip_translation(p)}
            skip_sequential_idxs = find_sequential_footnotes(paragraphs)
            # 페이지번호·그래프레이블 → bilingual에서 완전 제외 (미주로도 안 가고 삭제)
            drop_idxs = {i for i, p in enumerate(paragraphs) if should_drop_paragraph(p)}
            skip_all_idxs = (skip_section_idxs | skip_individual_idxs | skip_sequential_idxs) - drop_idxs
            # 이미 목표 언어인 단락 → 캐시에 사전 입력 (API 호출 없이 원문 그대로 보존)
            already_target_n = 0
            for p in paragraphs:
                if p not in cached and _paragraph_already_target(p):
                    cached[p] = p
                    already_target_n += 1
            resume_n = sum(1 for p in paragraphs if p in cached)
            if already_target_n:
                st.write(f"✅ 이미 목표 언어: {already_target_n}개 단락 — API 호출 생략")
            if resume_n - already_target_n > 0:
                st.write(f"♻️ 이전 번역 재사용: {resume_n - already_target_n}/{len(paragraphs)} 단락 — 신규 호출 {len(paragraphs)-resume_n}개")
            if drop_idxs:
                st.write(f"🗑️ 제외(페이지번호·레이블): {len(drop_idxs)}개 단락")
            if skip_all_idxs:
                st.write(f"⏭️ 번역 skip 대상: {len(skip_all_idxs)}/{len(paragraphs)} 단락")
            st.write(f"🌐 **2단계** · 영→한 번역 중 ({len(paragraphs)}단락, {_tr_label})…")
            N = len(paragraphs)
            prog = st.progress(0.0, text=f"0/{N} (0.0%)")
            bilingual: list = []
            failed_tr = cache_hits = api_calls = skipped_n = 0
            consecutive_fail = 0
            RATE_LIMIT_THRESHOLD = 3
            # 각주·인용은 본문 뒤로 모아 미주(尾註)로 — 읽기 흐름 보존 (2026-06-11)
            # drop_idxs(페이지번호·레이블)는 iter_order에서 아예 제외
            _iter_order = [i for i in range(N) if i not in skip_all_idxs and i not in drop_idxs] + \
                          [i for i in range(N) if i in skip_all_idxs]
            _endnote_marked = False
            try:
                import time as _time2
                for _seq, idx in enumerate(_iter_order):
                    para = paragraphs[idx]
                    # 일시정지 플래그 체크 (워커가 폴링)
                    while is_paused(txt_path.stem):
                        prog.progress(_seq / N, text=f"⏸️ 일시정지 중 ({_seq}/{N}) — ▶️ 재개 누르면 이어감")
                        _time2.sleep(2)
                    if idx in skip_all_idxs:
                        if not _endnote_marked:
                            bilingual.append("## Endnotes — collected footnotes & citations"
                                             "\n\n## 미주 — 각주·인용 모음 (원문 보존)")
                            _endnote_marked = True
                        bilingual.append(f"{para}\n\n(원문 보존: 각주·인용)")
                        skipped_n += 1
                        _save_bilingual_atomic(bilingual_path, bilingual)
                        _save_en_ko_split(bilingual_path, bilingual)
                        done = _seq + 1
                        prog.progress(done / N, text=f"{done}/{N} ({done/N*100:.1f}%) — ♻️ {cache_hits} / 🌐 {api_calls} / ⏭️ {skipped_n}" + (f" / ❌ {failed_tr}" if failed_tr else ""))
                        continue
                    ko = cached.get(para)
                    if ko is None:
                        ko = _tr_fn(para)
                        api_calls += 1
                    else:
                        cache_hits += 1
                    if ko:
                        # 번역 결과에서 '한글명(원어)' 패턴 수집 → 이후 단락은 한글만
                        for _ko_n, _en_n in _re.findall(
                                r"([가-힣]{2,}(?:[·\s][가-힣]{2,}){0,4})\(([A-Za-z][A-Za-z .'\-]{1,40})\)", ko):
                            _name_glossary.setdefault(_en_n.strip(), _ko_n.strip())
                        bilingual.append(f"{para}\n\n{ko}")
                        consecutive_fail = 0
                    else:
                        bilingual.append(para)
                        failed_tr += 1
                        if cached.get(para) is None:
                            consecutive_fail += 1
                    if consecutive_fail >= RATE_LIMIT_THRESHOLD:
                        _save_bilingual_atomic(bilingual_path, bilingual)
                        _save_en_ko_split(bilingual_path, bilingual)
                        append_log(f"RATE_LIMIT: 연속 {consecutive_fail}회 실패 — 자동 일시정지 ({uf.name}, {_seq+1}/{N})")
                        st.warning(f"⏸️ **Claude 한도 임박 추정** — 연속 {consecutive_fail}회 실패. 진행분({_seq+1}/{N}) 저장 후 자동 일시정지.")
                        break
                    _save_bilingual_atomic(bilingual_path, bilingual)
                    _save_en_ko_split(bilingual_path, bilingual)
                    done = _seq + 1
                    prog.progress(done / N, text=f"{done}/{N} ({done/N*100:.1f}%) — ♻️ {cache_hits} / 🌐 {api_calls} / ⏭️ {skipped_n}" + (f" / ❌ {failed_tr}" if failed_tr else ""))
            except Exception as e:
                _save_bilingual_atomic(bilingual_path, bilingual)
                _save_en_ko_split(bilingual_path, bilingual)
                append_log(f"ERROR: 번역 루프 예외 - {uf.name} ({len(bilingual)}/{len(paragraphs)} 단락, {type(e).__name__})")
                st.error(f"번역 중 예외 발생 — 진행분 {len(bilingual)}/{len(paragraphs)} 저장.")
                raise
            upload_file = bilingual_path
            _total_par = len(paragraphs)
            if failed_tr == _total_par and _total_par > 0:
                st.error(f"❌ **번역 전체 실패** ({failed_tr}/{_total_par}) — [KO] 0개. 임베드 자동 차단.")
            elif failed_tr:
                st.warning(f"⚠️ **{failed_tr}/{_total_par} 단락 번역 실패** — failed로 보내지 않고 **큐에 보류**합니다 (재번역 후 임베드 권장).")
            else:
                st.success(f"✅ 번역 완료 → `{bilingual_path.name}`")
            append_log(f"번역: {bilingual_path.name} ({_total_par-failed_tr}/{_total_par})")
            if failed_tr == _total_par and _total_par > 0:
                # 전체 실패만 failed 폴더로 이동 + 파이프라인 중단 (genuinely broken)
                success = False
                if dest.exists():
                    shutil.move(str(dest), str(FAILED_DIR / uf.name))
                append_log(f"ERROR: 번역 전체 실패로 중단 - {uf.name} ({failed_tr}/{_total_par} 단락)")
                status_ui.update(label=f"❌ 번역 전체 실패: {uf.name}", state="error")
            elif failed_tr:
                # 부분 실패: failed 미이동 → done 유지 + 큐로 라우팅(자동 임베드 차단).
                # OCR·MD 성과가 failed 폴더에 묻히지 않게. (2026-05-31 정책 변경)
                partial_fail_n = failed_tr
                defer_embed = True
                append_log(f"WARN: 번역 부분 실패 {failed_tr}/{_total_par} - {uf.name}: failed 미이동, 큐 보류 라우팅")
                status_ui.update(label=f"⚠️ 부분 실패 ({failed_tr}/{_total_par}) → 큐 보류: {uf.name}", state="complete")
            else:
                status_ui.update(label=f"✅ 번역 완료: {uf.name}", state="complete")
        elif do_translate and txt_path and txt_path.exists():
            st.write("ℹ️ 한국어 문서 감지 — 번역 스킵")
            if success:
                status_ui.update(label=f"✅ {uf.name} (번역 스킵)", state="complete")
        else:
            if success:
                status_ui.update(label=f"✅ {uf.name}", state="complete")

    # Phase 2 inline
    stages = {"ocr":"skip","txt":"skip","md":"skip","bilingual":"skip","anythingllm":"pending","wiki":"pending"}
    is_pdf = uf.name.lower().endswith(".pdf")
    if is_pdf:
        stages["ocr"] = "ok" if success and txt_path and txt_path.exists() else "fail"
    if txt_path and txt_path.exists() and txt_path.stat().st_size > 0:
        stages["txt"] = "ok"
    stages["md"] = "ok" if (md_src and md_src.exists()) else ("fail" if is_pdf else "skip")
    _bil = find_bilingual(ws_name, Path(uf.name).stem)
    if _bil is not None:
        stages["bilingual"] = "ok"

    if not success:
        st.warning(f"⏭️ **{uf.name}** — 이전 단계 실패로 임베드/Wiki 건너뜀. FAILED 폴더로 이동됨.")
        notify(f"{uf.name} 실패 (번역 중단)", title=ws_name)
        stages["anythingllm"] = "skip"
        stages["wiki"] = "skip"
        return {"name": uf.name, "ok": False, "ws": ws_name, "stages": stages,
                "pdf_path": str(FAILED_DIR / uf.name) if (FAILED_DIR / uf.name).exists() else "",
                "txt_path": str(RAW_DIR / ws_name / (Path(uf.name).stem + ".txt")),
                "md_path": "",
                "bilingual_path": str(_bil) if _bil is not None else ""}

    # ── 마무리 + Gemini 위키 (임베드/AnythingLLM 제거: 2026-06-09) ──
    with st.status(f"마무리·Wiki 생성: {uf.name}", expanded=True) as status_ui:
        if partial_fail_n:
            st.warning(f"⚠️ 번역 {partial_fail_n}단락 실패 — 그래도 Gemini가 TXT(원문/부분번역)로 노트 생성.")
        # PDF → DONE/pdf/
        done_sub = DONE_DIR / ws_name
        done_sub.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            pdf_save_dir = done_sub / PDF_SUB
            pdf_save_dir.mkdir(parents=True, exist_ok=True)
            final_pdf = pdf_save_dir / uf.name
            shutil.move(str(dest), str(final_pdf))
        # TXT·MD → DONE
        _src_txt = txt_path if (txt_path and txt_path.exists()) else None
        md_ok = bool(md_src and md_src.exists())
        if md_ok:
            txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
            md_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
            if _src_txt:
                final_txt = txt_dir(DONE_DIR, ws_name) / _src_txt.name
                shutil.move(str(_src_txt), str(final_txt))
            final_md = md_dir(DONE_DIR, ws_name) / md_src.name
            shutil.move(str(md_src), str(final_md))
        elif _src_txt:
            final_txt = done_sub / _src_txt.name
            shutil.move(str(_src_txt), str(final_txt))
        # Gemini 위키 생성 (책 전문 TXT → 옵시디언 노트)
        if not do_wiki:
            st.write("⏭️ 위키 저장 꺼짐 — Wiki 건너뜀")
            stages["wiki"] = "skip"
        elif final_txt and Path(final_txt).exists():
            st.write(f"📝 **Gemini 위키 생성** · `{Path(final_txt).name}`")
            stages["wiki"] = "pending" if trigger_gemini_wiki(final_txt) else "fail"
        else:
            st.write("⏭️ TXT 없음 — Wiki 건너뜀")
            stages["wiki"] = "skip"
        stages["anythingllm"] = "removed"
        append_log(f"완료: {uf.name}")
        if final_txt and Path(final_txt).exists():
            queue_add("tab2_ready", [_nfc(Path(final_txt).stem)])  # → 장별분할 큐
        status_ui.update(label=f"✅ 완료: {uf.name}", state="complete")

    notify(f"{uf.name} {'완료' if success else '실패'}", title=ws_name)
    bilingual_p = find_bilingual(ws_name, Path(uf.name).stem)
    if bilingual_p is not None:
        stages["bilingual"] = "ok"
    return {
        "name": uf.name, "ok": success, "ws": ws_name, "stages": stages,
        "pdf_path": str(final_pdf) if final_pdf else "",
        "txt_path": str(final_txt) if final_txt else "",
        "md_path": str(final_md) if final_md else "",
        "bilingual_path": str(bilingual_p) if bilingual_p is not None else "",
    }


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


def _move_unassigned_to_ws(stem: str, new_ws: str) -> int:
    """_unassigned 아래의 stem 관련 파일을 new_ws로 이동. 이동 건수 반환. (2026-05-18)"""
    src_ws_dir = DONE_DIR / "_unassigned"
    dst_ws_dir = DONE_DIR / new_ws
    if not src_ws_dir.exists():
        return 0
    moved = 0
    pairs = [
        (src_ws_dir / f"{stem}.pdf",                                 dst_ws_dir / f"{stem}.pdf"),
        (src_ws_dir / MD_SUB         / f"{stem}.md",                  dst_ws_dir / MD_SUB         / f"{stem}.md"),
        (src_ws_dir / TXT_SUB        / f"{stem}.txt",                 dst_ws_dir / TXT_SUB        / f"{stem}.txt"),
        (src_ws_dir / TRANS_SUB / f"{stem}_bilingual.txt",       dst_ws_dir / TRANS_SUB / f"{stem}_bilingual.txt"),
    ]
    for src, dst in pairs:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except Exception as e:
                append_log(f"WARN: _unassigned→{new_ws} 이동 실패 ({src.name}): {e}")
    return moved


def load_pipeline_results() -> list:
    if not RESULTS_FILE.exists():
        return []
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pipeline_results(results: list):
    try:
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_FILE.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def read_log(n: int = 20) -> list:
    if not LOG_FILE.exists():
        return []
    return LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]


def open_path(p: Path, reveal: bool = False):
    """파일을 OS 기본 앱으로 열기. reveal=폴더에서 선택 표시.
    (2026-06-11 윈도우 수정 — 'open'은 맥 전용)"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(p)] if reveal else ["open", str(p)])
        elif reveal:
            # 리스트로 넘기면 인자 전체가 따옴표로 감싸여 explorer가 무시하고
            # 문서 폴더를 열어버림 — 경로만 따옴표한 문자열로 직접 구성 (2026-06-11)
            subprocess.run(f'explorer /select,"{p}"')
        else:
            os.startfile(str(p))
    except Exception as e:
        append_log(f"WARN: 파일 열기 실패 ({type(e).__name__}) {str(e)[:120]}")


def _obsidian_config() -> Path:
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


DEFAULT_WS = "My Bookshelf"   # 단일 기본 폴더

# ── 파이프라인 큐 ────────────────────────────────────────────
# 각 탭이 완료한 항목을 다음 탭 큐에 등록하는 단방향 파이프라인.
# 큐 파일: done/My Bookshelf/.pipeline_queue.json
# 단계: tab2_ready(분할), tab3_ready(번역), tab4_ready(요약), tab5_ready(Wiki)

_QUEUE_FILE = DONE_DIR / DEFAULT_WS / ".pipeline_queue.json"
_QUEUE_STAGES = ["tab2_ready", "tab3_ready", "tab4_ready", "tab4_failed", "tab5_ready"]

def _q_load() -> dict:
    try:
        return json.loads(_QUEUE_FILE.read_text(encoding="utf-8")) if _QUEUE_FILE.exists() else {}
    except Exception:
        return {}

def _q_save(data: dict) -> None:
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def queue_list(stage: str) -> list[str]:
    """큐에서 해당 단계 항목 목록 반환."""
    return _q_load().get(stage, [])

def queue_add(stage: str, items: list[str]) -> None:
    """큐에 항목 추가 (중복 제거)."""
    d = _q_load()
    cur = d.get(stage, [])
    for it in items:
        if it not in cur:
            cur.append(it)
    d[stage] = cur
    _q_save(d)

def queue_remove(stage: str, items: list[str]) -> None:
    """큐에서 항목 제거."""
    d = _q_load()
    cur = d.get(stage, [])
    d[stage] = [x for x in cur if x not in set(items)]
    _q_save(d)

def queue_clear(stage: str) -> None:
    d = _q_load()
    d[stage] = []
    _q_save(d)


_HANGUL_RE = _re.compile(r'[가-힣ᄀ-ᇿ㄰-㆏]')

def _needs_translation(stem: str) -> bool:
    """책 제목(stem)에 한글이 없으면 번역 필요(영문 등), 한글 있으면 번역 불필요."""
    return not bool(_HANGUL_RE.search(stem))


def open_wiki_vault():
    """위키 폴더를 옵시디언 보관함(Vault)로 등록 후 옵시디언으로 열기. 실패 시 폴더라도 연다."""
    ensure_obsidian_vault(WIKI_DIR)
    from urllib.parse import quote
    uri = "obsidian://open?path=" + quote(str(WIKI_DIR.resolve()))
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", uri])
        else:
            os.startfile(uri)
    except Exception:
        open_path(WIKI_DIR)


def notify(msg: str, title: str = "My Bookshelf"):
    if sys.platform != "darwin":   # 윈도우 등: OS 알림 생략 (UI 토스트가 이미 표시됨)
        return
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{msg}" with title "{title}" sound name "Glass"'],
        capture_output=True,
    )


# ─── 단계별 처리 헬퍼 ──────────────────────────────────────

def chapters_dir(ws_name: str, stem: str) -> Path:
    return DONE_DIR / ws_name / "chapters" / stem


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


def split_book_to_chapters(ws_name: str, stem: str) -> tuple[int, str]:
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
    mode, chapters = _cw.chapter_split(md_text, txt_text)
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


def translate_one_chapter(ch_path: Path, engine: str, progress_cb=None) -> tuple[bool, str]:
    """단일 챕터 TXT 번역 → _ko.txt 저장. (ok, msg)."""
    try:
        text = ch_path.read_text(encoding="utf-8", errors="ignore")
        ko_path = ch_path.with_name(ch_path.stem + "_ko.txt")
        if not needs_translation(ch_path):
            ko_path.write_text(text, encoding="utf-8")
            return True, "이미 한국어 — 그대로 복사"
        paras = _split_paragraphs_robust(text)
        out: list[str] = []
        translated_n = preserved_n = dropped_n = failed_n = 0
        total = len(paras) or 1
        for idx, p in enumerate(paras, 1):
            if should_drop_paragraph(p):
                dropped_n += 1
                if progress_cb:
                    progress_cb(idx, total, translated_n, preserved_n, dropped_n, failed_n)
                continue
            if should_skip_translation(p):
                out.append(p)
                preserved_n += 1
            else:
                ko = translate(p, engine)
                if _translation_is_valid(p, ko):
                    out.append(ko)
                    translated_n += 1
                else:
                    out.append(p)
                    failed_n += 1
            if progress_cb:
                progress_cb(idx, total, translated_n, preserved_n, dropped_n, failed_n)
        detail = f"{len(out)}단락 처리 완료 · 번역 {translated_n} · 원문보존 {preserved_n}"
        if dropped_n:
            detail += f" · 삭제 {dropped_n}"
        if failed_n:
            detail += f" · 실패보존 {failed_n}"
        if translated_n == 0:
            ko_path.unlink(missing_ok=True)
            return False, detail + " — 유효한 한국어 번역 결과가 없습니다"
        ko_path.write_text("\n\n".join(out), encoding="utf-8")
        return True, detail
    except Exception as e:
        return False, str(e)[:200]


def _safe_source_stem(source: str, fallback: str = "paper") -> str:
    stem = _re.sub(r"^https?://", "", source.strip(), flags=_re.I)
    stem = _re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", stem).strip("._-")
    return (stem[:90] or fallback)


def _paper_source_candidates(source: str) -> list[str]:
    """논문 출처(URL/DOI/arXiv)에서 다운로드를 시도할 후보 URL 목록."""
    from urllib.parse import quote

    src = source.strip()
    if not src:
        return []
    candidates: list[str] = []
    arxiv = _re.search(r"(?:arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?", src, _re.I)
    if arxiv:
        candidates.append(f"https://arxiv.org/pdf/{arxiv.group(1)}")
    if src.lower().startswith(("http://", "https://")):
        candidates.append(src)
    elif src.lower().startswith("doi:"):
        candidates.append("https://doi.org/" + quote(src[4:].strip(), safe="/.()"))
    elif src.startswith("10.") and "/" in src:
        candidates.append("https://doi.org/" + quote(src, safe="/.()"))
    return list(dict.fromkeys(candidates))


def _response_filename(resp, fallback_stem: str, suffix: str) -> str:
    from urllib.parse import unquote, urlparse

    cd = resp.headers.get("Content-Disposition", "")
    m = _re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, _re.I)
    if m:
        name = unquote(m.group(1)).strip()
    else:
        name = Path(urlparse(resp.geturl()).path).name or fallback_stem + suffix
    if not Path(name).suffix:
        name += suffix
    return _re.sub(r'[/\\:*?"<>|]', "_", name)


def _extract_pdf_link_from_html(html: str, base_url: str) -> str | None:
    from urllib.parse import urljoin

    patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    ]
    for pat in patterns:
        m = _re.search(pat, html, _re.I)
        if m:
            return urljoin(base_url, m.group(1).replace("&amp;", "&"))
    return None


def _download_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def download_paper_source(source: str) -> tuple[bool, Path | None, str]:
    """논문 출처가 실제 다운로드 가능한 PDF/TXT인지 확인하고 임시 파일로 저장."""
    candidates = _paper_source_candidates(source)
    if not candidates:
        return False, None, "URL/DOI/arXiv 형식이 아닙니다"
    fallback_stem = _safe_source_stem(source)
    headers = {
        "User-Agent": "MyBookshelf/0.6 (+https://localhost)",
        "Accept": "application/pdf,text/plain,text/html;q=0.8,*/*;q=0.5",
    }
    last_reason = "다운로드 가능한 PDF/TXT 링크를 찾지 못했습니다"
    seen: set[str] = set()
    ssl_context = _download_ssl_context()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20, context=ssl_context) as resp:
                data = resp.read(25 * 1024 * 1024 + 1)
                if len(data) > 25 * 1024 * 1024:
                    return False, None, "파일이 25MB를 초과합니다"
                ctype = (resp.headers.get("Content-Type") or "").lower()
                final_url = resp.geturl()
                if data.startswith(b"%PDF") or "application/pdf" in ctype:
                    name = _response_filename(resp, fallback_stem, ".pdf")
                    out = Path(tempfile.gettempdir()) / name
                    out.write_bytes(data)
                    return True, out, ""
                if "text/plain" in ctype:
                    name = _response_filename(resp, fallback_stem, ".txt")
                    out = Path(tempfile.gettempdir()) / name
                    out.write_bytes(data)
                    return True, out, ""
                if "html" in ctype or data[:512].lstrip().lower().startswith(b"<!doctype html") or b"<html" in data[:2048].lower():
                    html = data.decode("utf-8", errors="ignore")
                    pdf_url = _extract_pdf_link_from_html(html, final_url)
                    if pdf_url and pdf_url not in seen:
                        candidates.append(pdf_url)
                    else:
                        last_reason = "페이지는 열리지만 PDF 다운로드 링크를 찾지 못했습니다"
                else:
                    last_reason = f"지원하지 않는 응답 형식입니다: {ctype or '알 수 없음'}"
        except urllib.error.HTTPError as e:
            last_reason = f"서버가 HTTP {e.code}로 거부했습니다"
        except urllib.error.URLError as e:
            last_reason = f"네트워크 오류: {getattr(e, 'reason', e)}"
        except Exception as e:
            last_reason = f"{type(e).__name__}: {str(e)[:160]}"
    return False, None, last_reason


def translate_downloaded_paper(source_file: Path, engine: str, progress_cb=None) -> tuple[bool, str]:
    """다운로드한 논문 파일을 TXT로 준비한 뒤 한국어 번역본을 저장."""
    try:
        txt_dir(DONE_DIR, DEFAULT_WS).mkdir(parents=True, exist_ok=True)
        pdf_dir = DONE_DIR / DEFAULT_WS / PDF_SUB
        pdf_dir.mkdir(parents=True, exist_ok=True)
        if source_file.suffix.lower() == ".pdf":
            txt_path, _md, err = pdf_to_txt(source_file)
            if not txt_path:
                return False, f"PDF 텍스트 추출 실패: {err}"
            final_pdf = pdf_dir / source_file.name
            shutil.copy2(str(source_file), str(final_pdf))
            final_txt = txt_dir(DONE_DIR, DEFAULT_WS) / (source_file.stem + ".txt")
            shutil.move(str(txt_path), str(final_txt))
        else:
            final_txt = txt_dir(DONE_DIR, DEFAULT_WS) / source_file.name
            shutil.copy2(str(source_file), str(final_txt))
        ok, msg = translate_one_chapter(final_txt, engine, progress_cb=progress_cb)
        if ok:
            queue_add("tab4_ready", [str(final_txt.relative_to(DONE_DIR))])
            return True, f"{msg} → {final_txt.with_name(final_txt.stem + '_ko.txt').name}"
        return False, msg
    except Exception as e:
        return False, str(e)[:200]


def summarize_one_chapter(ch_path: Path, book_stem: str) -> tuple[bool, str]:
    """단일 챕터 TXT → 위키 JSON 요약. _wiki.json 저장. (ok, summary snippet)."""
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
        (ch_path.with_name(ch_path.stem + "_wiki.json")).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True, (data.get("summary") or "")[:120]
    except Exception as e:
        msg = str(e)[:300]
        try:
            append_log(f"ERROR: 장별 요약 실패 - {ch_path.name} ({type(e).__name__}) {msg}")
        except Exception:
            pass
        return False, msg[:200]


def _ch_link(stem: str, ch_title: str) -> str:
    """챕터 노트의 Obsidian 위키링크 문자열 반환."""
    try:
        import gemini_wiki as _gw
        return "[[" + _gw.make_filename(_gw.nfc(f"{stem} — {ch_title}"))[:-3] + "]]"
    except Exception:
        return f"[[{stem} — {ch_title}]]"


def build_single_chapter_wiki(ws_name: str, stem: str, json_path: Path, wiki_dir: Path | None = None) -> tuple[bool, str]:
    """단일 챕터 _wiki.json → 개별 Obsidian 노트 (전체 요약 노트와 링크 연결). (ok, path or msg)."""
    try:
        import gemini_wiki as _gw
    except ImportError as e:
        return False, f"임포트 실패: {e}"
    try:
        d = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"JSON 읽기 실패: {e}"
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
    """챕터 _wiki.json들 → 옵시디언 위키 노트 생성. (ok, path or msg)."""
    try:
        import chapter_wiki as _cw
        import gemini_wiki as _gw
    except ImportError as e:
        return False, f"임포트 실패: {e}"
    ch_dir = chapters_dir(ws_name, stem)
    if not ch_dir.exists():
        return False, "챕터 폴더 없음 — 1단계를 먼저 실행하세요"
    json_files = sorted(ch_dir.glob("*_wiki.json"))
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
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
            title = _re.sub(r"^\d+_", "", jf.stem.replace("_wiki", ""))
            sections.append({"idx": i, "title": title,
                             "summary": d.get("summary", ""),
                             "body": d.get("body", "")})
        except Exception:
            continue
    if not sections:
        return False, "유효한 요약 없음"
    ov = _cw.generate_overview(stem, sections)
    cat  = ov.get("category", "기타")
    intro = ov.get("intro", "")
    summ  = ov.get("summary", "")
    today = __import__("datetime").date.today().isoformat()
    prov, model = llm.wiki_provider_model()
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


# ─── TXT 단독 처리 (번역·위키 생략) ─────────────────────────

def _do_ocr_only(uf, ws_name: str, fast: bool = False) -> dict:
    """PDF → TXT 변환만 수행. fast=True이면 pdftotext 직접 추출."""
    dest = UPLOAD_TMP / uf.name
    _src = getattr(uf, "_p", None)
    if not (_src and Path(_src).resolve() == dest.resolve()):
        uf.seek(0)
        with open(dest, "wb") as f:
            f.write(uf.read())
    done_sub = DONE_DIR / ws_name
    done_sub.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() != ".pdf":
        txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        final = txt_dir(DONE_DIR, ws_name) / dest.name
        shutil.move(str(dest), str(final))
        append_log(f"TXT 직접 업로드: {final.name}")
        queue_add("tab2_ready", [_nfc(Path(final).stem)])   # → 장별분할 큐
        return {"ok": True, "name": uf.name, "txt_path": str(final), "md_path": "", "error": ""}
    txt_path, md_src, err = pdf_to_txt(dest, fast=fast)
    if not txt_path:
        try: shutil.move(str(dest), str(FAILED_DIR / uf.name))
        except Exception: pass
        append_log(f"ERROR: TXT 변환 실패 — {uf.name}: {err}")
        return {"ok": False, "name": uf.name, "txt_path": "", "md_path": "", "error": err}
    pdf_save_dir2 = done_sub / PDF_SUB
    pdf_save_dir2.mkdir(parents=True, exist_ok=True)
    final_pdf = pdf_save_dir2 / uf.name
    shutil.move(str(dest), str(final_pdf))
    txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
    final_txt = txt_dir(DONE_DIR, ws_name) / txt_path.name   # 항상 1_txt/에 저장
    shutil.move(str(txt_path), str(final_txt))
    if md_src and md_src.exists():
        md_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        final_md = md_dir(DONE_DIR, ws_name) / md_src.name
        shutil.move(str(md_src), str(final_md))
    else:
        final_md = None
    append_log(f"TXT 변환 완료: {uf.name} → {Path(final_txt).name}")
    queue_add("tab2_ready", [_nfc(Path(final_txt).stem)])   # → 장별분할 큐 등록
    return {"ok": True, "name": uf.name, "txt_path": str(final_txt),
            "md_path": str(final_md) if final_md else "", "error": ""}


# ── UI ────────────────────────────────────────────────────

def _find_app_icon(name: str) -> Path | None:
    """MyBookshelf.iconset/<name>을 여러 후보 위치에서 찾는다.
    - 개발 트리: core/ 의 부모(레포 루트)
    - .app 번들: Resources/ (pipeline_app.py와 같은 폴더)
    - SSD 실행본: pipeline_app.py와 같은 폴더"""
    here = Path(__file__).resolve().parent
    for base in (here.parent, here, here.parent / "platform" / "mac"):
        p = base / "MyBookshelf.iconset" / name
        if p.exists():
            return p
    return None

_icon_path = _find_app_icon("icon_32x32.png")
_page_icon = str(_icon_path) if _icon_path else "📚"
st.set_page_config(page_title="My Bookshelf", page_icon=_page_icon, layout="wide")

if "ui_font_scale" not in st.session_state:
    st.session_state["ui_font_scale"] = 1.0

def _font_scale_controls():
    cur = float(st.session_state.get("ui_font_scale", 1.0))
    c1, c2, c3 = st.columns([0.75, 1, 0.75])
    if c1.button("-", key="font_size_minus", use_container_width=True, help="글자 크기 줄이기"):
        st.session_state["ui_font_scale"] = max(0.85, round(cur - 0.05, 2))
        st.rerun()
    c2.markdown(
        f"<div style='text-align:center;color:#6b7280;font-size:0.82rem;line-height:2.35'>"
        f"{int(cur * 100)}%</div>",
        unsafe_allow_html=True,
    )
    if c3.button("+", key="font_size_plus", use_container_width=True, help="글자 크기 키우기"):
        st.session_state["ui_font_scale"] = min(1.35, round(cur + 0.05, 2))
        st.rerun()

# 로딩 오버레이 — 세션 최초 진입 시에만 표시 (LLM 작업 중 재렌더링 때는 건너뜀)
_loading_ph = st.empty()

def _loading_step(msg: str, sub: str = "잠시만 기다려 주세요") -> None:
    """로딩 오버레이 메시지 갱신. 첫 진입 시에만 동작."""
    if st.session_state.get("_app_loaded"):
        return
    _loading_ph.markdown(
        "<div style='position:fixed;top:0;left:0;width:100%;height:100%;"
        "background:rgba(255,255,255,0.96);z-index:9999;"
        "display:flex;justify-content:center;align-items:center;"
        "flex-direction:column;gap:14px'>"
        "<div style='font-size:2.4rem'>📚</div>"
        f"<div style='font-size:1.15rem;color:#374151;font-weight:600'>{msg}</div>"
        f"<div style='color:#9ca3af;font-size:0.88rem'>{sub}</div>"
        "</div>",
        unsafe_allow_html=True,
    )

_loading_step("My Bookshelf 실행 중…")

# ── 글로벌 스타일 (2026-05-18 v2 — Linear·Vercel 톤) ────────────
# 잔잔한 segmented control + 모노톤 칩. 선택된 것만 도드라지는 미감.
_ui_font_scale = float(st.session_state.get("ui_font_scale", 1.0))
st.markdown("""
<style>
:root {
    --mb-font-scale: __MB_FONT_SCALE__;
}
/* 앱 상단 기본 여백 축소 */
[data-testid="stHeader"],
header[data-testid="stHeader"] {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    background: transparent !important;
}
[data-testid="stToolbar"],
[data-testid="stDecoration"],
#MainMenu {
    display: none !important;
}
.block-container,
[data-testid="stAppViewContainer"] .block-container,
[data-testid="stAppViewContainer"] section.main .block-container {
    padding-top: 1.25rem !important;
    padding-bottom: 2.25rem !important;
    margin-top: 0 !important;
}

/* === 탭 — Segmented Control (macOS/iOS 영감) === */
.stTabs [data-baseweb="tab-list"] {
    gap: 2px;
    background-color: rgba(0, 0, 0, 0.04);
    padding: 4px;
    border-radius: 10px;
    border: 1px solid rgba(0, 0, 0, 0.05);
    display: inline-flex;
    margin-bottom: 16px;
}
.stTabs [data-baseweb="tab-list"] [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-list"] [data-baseweb="tab-border"] {
    display: none !important;
}
.stTabs [data-baseweb="tab"] {
    height: 38px;
    padding: 0 18px;
    background-color: transparent;
    border: none !important;
    border-radius: 7px;
    color: #6b7280;
    transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
}
.stTabs [data-baseweb="tab"] p {
    font-size: 14.5px !important;
    font-weight: 500 !important;
    margin: 0 !important;
    letter-spacing: -0.008em;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #1f2937;
    background-color: rgba(255, 255, 255, 0.55);
}
.stTabs [aria-selected="true"] {
    background-color: white !important;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06),
                0 1px 2px rgba(0, 0, 0, 0.04);
}
.stTabs [aria-selected="true"] p {
    color: #111827 !important;
    font-weight: 600 !important;
}

/* === 라디오 — 모노톤 칩 (Vercel/Linear 영감) === */
div[data-testid="stRadio"] > label > div > p {
    font-size: 14px !important;
    font-weight: 500 !important;
    color: #6b7280 !important;
    margin-bottom: 10px !important;
    letter-spacing: -0.005em;
    text-transform: uppercase;
    font-size: 12px !important;
    letter-spacing: 0.05em;
}
div[data-testid="stRadio"] div[role="radiogroup"] {
    gap: 6px;
    flex-wrap: wrap;
}
div[data-testid="stRadio"] label[data-baseweb="radio"] {
    padding: 7px 13px;
    background-color: white;
    border: 1px solid rgba(0, 0, 0, 0.1);
    border-radius: 7px;
    transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
    cursor: pointer;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
}
div[data-testid="stRadio"] label[data-baseweb="radio"]:hover {
    background-color: #fafafa;
    border-color: rgba(0, 0, 0, 0.22);
    transform: translateY(-1px);
    box-shadow: 0 2px 5px rgba(0, 0, 0, 0.04);
}
div[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
    display: none;
}
div[data-testid="stRadio"] label[data-baseweb="radio"] > div:last-child p {
    font-size: 13.5px !important;
    font-weight: 500 !important;
    color: #4b5563 !important;
    margin: 0 !important;
    letter-spacing: -0.005em;
}
div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) {
    background-color: #111827;
    border-color: #111827;
    box-shadow: 0 1px 3px rgba(17, 24, 39, 0.18),
                0 1px 2px rgba(17, 24, 39, 0.12);
}
div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) > div:last-child p {
    color: white !important;
    font-weight: 600 !important;
}

/* === dataframe·container 유동 높이 (viewport 기반, 2026-05-18) === */
[data-testid="stDataFrame"] {
    height: calc(100vh - 280px) !important;
    min-height: 400px !important;
}
[data-testid="stDataFrame"] > div {
    height: 100% !important;
}

/* === 다크모드 자동 대응 === */
@media (prefers-color-scheme: dark) {
    .stTabs [data-baseweb="tab-list"] {
        background-color: rgba(255, 255, 255, 0.04);
        border-color: rgba(255, 255, 255, 0.07);
    }
    .stTabs [data-baseweb="tab"] {
        color: #9ca3af;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: #e5e7eb;
        background-color: rgba(255, 255, 255, 0.04);
    }
    .stTabs [aria-selected="true"] {
        background-color: rgba(255, 255, 255, 0.08) !important;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4) !important;
    }
    .stTabs [aria-selected="true"] p {
        color: #f3f4f6 !important;
    }

    div[data-testid="stRadio"] label[data-baseweb="radio"] {
        background-color: rgba(255, 255, 255, 0.03);
        border-color: rgba(255, 255, 255, 0.08);
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"]:hover {
        background-color: rgba(255, 255, 255, 0.06);
        border-color: rgba(255, 255, 255, 0.16);
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"] > div:last-child p {
        color: #9ca3af !important;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) {
        background-color: #f3f4f6;
        border-color: #f3f4f6;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) > div:last-child p {
        color: #111827 !important;
    }
}

/* === 우상단 툴바 (2026-06-11) === */
/* Deploy 버튼 숨김 — 로컬 앱에는 의미 없음 */
[data-testid="stAppDeployButton"] { display: none !important; }
/* 실행 중 Stop 버튼 — 한글 라벨 + 눈에 띄는 빨강 */
[data-testid="stStatusWidget"] button {
    font-size: 0 !important;
    background: #e5484d !important;
    border: none !important;
    border-radius: 8px !important;
    padding: 4px 12px !important;
    min-height: 28px;
}
[data-testid="stStatusWidget"] button::after {
    content: "⏹ 중지";
    font-size: 0.85rem;
    font-weight: 600;
    color: #ffffff;
}
[data-testid="stStatusWidget"] button:hover {
    background: #d93036 !important;
}

/* 사용자 글자 크기 조절 */
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stMarkdownContainer"] span,
[data-testid="stMarkdownContainer"] div,
[data-testid="stText"],
[data-testid="stCaptionContainer"],
label,
input,
textarea,
.stButton button,
[data-testid="stSelectbox"] *,
[data-testid="stRadio"] *,
[data-testid="stCheckbox"] *,
[data-testid="stMetric"] * {
    font-size: calc(1em * var(--mb-font-scale)) !important;
}
[data-testid="stMarkdownContainer"] h1 {
    font-size: calc(2.0rem * var(--mb-font-scale)) !important;
}
[data-testid="stMarkdownContainer"] h2 {
    font-size: calc(1.55rem * var(--mb-font-scale)) !important;
}
[data-testid="stMarkdownContainer"] h3 {
    font-size: calc(1.28rem * var(--mb-font-scale)) !important;
}
[data-testid="stMarkdownContainer"] h4 {
    font-size: calc(1.08rem * var(--mb-font-scale)) !important;
}
</style>
""".replace("__MB_FONT_SCALE__", str(_ui_font_scale)), unsafe_allow_html=True)

_logo_path = _find_app_icon("icon_128x128.png")
if _logo_path:
    import base64 as _b64
    _logo_b64 = _b64.b64encode(_logo_path.read_bytes()).decode()
    _logo_html = f'<img src="data:image/png;base64,{_logo_b64}" width="52" style="vertical-align:middle;margin-right:10px">'
else:
    _logo_html = "📚 "
_brand_col, _font_col = st.columns([6, 1.6])
_brand_col.markdown(
    f"# {_logo_html}My Bookshelf <span style='font-size:0.42em;color:#9aa0a6;"
    f"font-weight:400;vertical-align:middle'>{APP_VERSION}</span>",
    unsafe_allow_html=True,
)
with _font_col:
    _font_scale_controls()
st.caption("PDF → TXT변환 → 장별 분할 → 번역 → 요약 → Obsidian Wiki")

_loading_step("파일 목록 확인 중…", "처리된 파일과 API 설정을 읽고 있습니다")

# ── 상태 배너 ────────────────────────────────────────────
_avail_api_providers = [llm.PROVIDERS[p]["label"] for p in llm.API_PROVIDERS if llm.has_key(p)]
_avail_cli_providers = [llm.PROVIDERS[p]["label"] for p in llm.CLI_PROVIDERS if llm.has_key(p)]
_avail_ai_providers = _avail_api_providers + _avail_cli_providers
_wiki_key_ok = bool(_avail_ai_providers)
wg_ok = wiki_generator_running()
col_s1, col_s2, col_s3, col_s4 = st.columns(4)
col_s1.metric("API 키", f"{len(_avail_api_providers)}개" if _avail_api_providers else "❌ 없음")
col_s2.metric("CLI 구독", f"{len(_avail_cli_providers)}개" if _avail_cli_providers else "없음")
col_s3.metric("위키 생성기", "🔄 생성 중" if wg_ok else "대기")
col_s4.metric("Wiki 완성", sum(1 for _ in WIKI_DIR.rglob("*.md")))
if not _avail_ai_providers:
    st.error("⚠️ 사용 가능한 AI가 없습니다 — ⚙️ 설정 탭에서 API 키를 입력하거나 CLI 구독 도구를 활성화하세요.")

# ── 초기 메뉴 ─────────────────────────────────────────────
TASKS = [
    ("1_txt", "📄 1·TXT변환", "PDF/TXT를 처리 대기열에 올리고 텍스트로 저장"),
    ("2_split", "📂 2·장별분할", "책 TXT를 챕터 단위 파일로 분리"),
    ("3_translate", "🌐 3·번역", "챕터 또는 논문 출처를 한국어로 번역"),
    ("4_summary", "📝 4·요약MD", "챕터별 위키 요약 JSON 생성"),
    ("5_wiki", "📖 5·Wiki반영", "요약을 Obsidian Wiki 노트로 저장"),
    ("settings", "⚙️ 설정", "API 키와 위키 생성 모델 설정"),
    ("all_run", "🚀 전체 실행", "TXT변환 → 장별분할 → 번역 → 요약 → Wiki를 한 번에 실행"),
]

_active_view = st.session_state.get("active_view")
if not _active_view:
    st.markdown("""
<style>
.menu-card {
    display: block;
    width: 100%;
    padding: 13px 17px;
    margin: 0 0 10px 0;
    border: 1px solid rgba(0, 0, 0, 0.12);
    border-radius: 10px;
    background: #ffffff;
    color: inherit !important;
    text-decoration: none !important;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
    transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
}
.menu-card:hover {
    border-color: rgba(0, 0, 0, 0.28);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
    transform: translateY(-1px);
}
.menu-title {
    display: block;
    font-size: 1.28rem;
    font-weight: 800;
    line-height: 1.25;
}
.menu-desc {
    display: block;
    margin-top: 3px;
    color: #6b7280;
    font-size: 0.96rem;
    line-height: 1.25;
}
</style>
""", unsafe_allow_html=True)
    st.markdown("#### 작업 메뉴")
    for _tid, _title, _desc in TASKS:
        _clicked = st.query_params.get("view") == _tid
        st.markdown(
            f'<a class="menu-card" href="?view={_tid}" target="_self">'
            f'<span class="menu-title">{_title}</span>'
            f'<span class="menu-desc">{_desc}</span>'
            f'</a>',
            unsafe_allow_html=True,
        )
        if _clicked:
            st.session_state["active_view"] = _tid
            st.query_params.clear()
            if _tid == "all_run":
                st.session_state["ocr_mode"] = "🚀 전체 실행 (TXT변환→장별분할→번역(영어문서인 경우)→Wiki)"
            st.rerun()
    _loading_ph.empty()
    st.session_state["_app_loaded"] = True
    st.stop()

_task_title = next((title for tid, title, _ in TASKS if tid == _active_view), "작업")
_top_l, _top_menu, _top_prev, _top_next, _top_skip = st.columns([5, 1, 1.15, 1.25, 1.45])
_top_l.markdown(f"### {_task_title}")
if _top_menu.button("← 메뉴", key="back_to_menu", use_container_width=True):
    st.session_state.pop("active_view", None)
    st.rerun()
_PREV_STEPS = {
    "2_split": ("1_txt", "이전: 1·TXT"),
    "3_translate": ("2_split", "이전: 2·분할"),
    "4_summary": ("3_translate", "이전: 3·번역"),
    "5_wiki": ("4_summary", "이전: 4·요약"),
}
_NEXT_STEPS = {
    "1_txt": [("2_split", "다음: 2·장별분할")],
    "2_split": [("3_translate", "다음: 3·번역"), ("4_summary", "건너뛰기: 4·요약MD")],
    "3_translate": [("4_summary", "다음: 4·요약MD")],
    "4_summary": [("5_wiki", "다음: 5·Wiki반영")],
}
if _active_view in _PREV_STEPS:
    _prev_view, _prev_label = _PREV_STEPS[_active_view]
    if _top_prev.button(_prev_label, key=f"prev_to_{_prev_view}", use_container_width=True):
        st.session_state["active_view"] = _prev_view
        st.rerun()
_next_steps_for_view = _NEXT_STEPS.get(_active_view, [])
if _next_steps_for_view:
    _next_view, _next_label = _next_steps_for_view[0]
    if _top_next.button(_next_label, key=f"next_to_{_next_view}", use_container_width=True):
        st.session_state["active_view"] = _next_view
        st.rerun()
    if len(_next_steps_for_view) > 1:
        _skip_view, _skip_label = _next_steps_for_view[1]
        if _top_skip.button(_skip_label, key=f"next_to_{_skip_view}", use_container_width=True):
            st.session_state["active_view"] = _skip_view
            st.rerun()



# ─── 공용 헬퍼 ───────────────────────────────────────────


def _checklist(items: list[dict], prefix: str, height: int = 320) -> list:
    """체크박스 파일 목록. items=[{"key":str,"label":str,"meta":str,"obj":any}]
    Returns: 선택된 obj 목록."""
    h1, h2, h3 = st.columns([1.3, 1, 4])
    if h1.button("✅ 전체 선택", key=f"{prefix}_sa", use_container_width=True):
        for it in items:
            st.session_state[f"{prefix}_{it['key']}"] = True
        st.rerun()
    if h2.button("⬜ 해제", key=f"{prefix}_da", use_container_width=True):
        for it in items:
            st.session_state[f"{prefix}_{it['key']}"] = False
        st.rerun()
    h3.caption(f"총 {len(items)}개")
    selected = []
    with st.container(height=height, border=True):
        for it in items:
            k = f"{prefix}_{it['key']}"
            c1, c2 = st.columns([0.05, 0.95])
            chk = c1.checkbox(" ", key=k, label_visibility="collapsed")
            c2.markdown(
                f"**{it['label']}** &nbsp;<small style='color:#9ca3af'>{it['meta']}</small>",
                unsafe_allow_html=True,
            )
            if chk:
                selected.append(it["obj"])
    return selected


def _wiki_model_radio(key: str) -> tuple[str, str]:
    """사용 가능한 AI 모델 radio 선택기. (prov, model) 반환.
    선택이 현재 wiki_provider_model과 다르면 자동으로 set_wiki_model 호출."""
    _avail = [(p, m)
              for p, info in llm.PROVIDERS.items()
              if llm.has_key(p)
              for m in info["models"]]
    if not _avail:
        st.warning("사용 가능한 AI 없음 — ⚙️ 설정 탭에서 API 키를 입력하세요.")
        return llm.wiki_provider_model()
    _wp, _wm = llm.wiki_provider_model()
    _labels = [f"{llm.PROVIDERS[p]['label']} · {m}" for p, m in _avail]
    _cur = f"{llm.PROVIDERS.get(_wp, {}).get('label', _wp)} · {_wm}"
    _idx = _labels.index(_cur) if _cur in _labels else 0
    _sel = st.radio("🤖 AI 모델", _labels, index=_idx, horizontal=True, key=key)
    _p, _m = _avail[_labels.index(_sel)]
    if (_p, _m) != (_wp, _wm):
        llm.set_wiki_model(_p, _m)
    return _p, _m


_loading_step("화면 구성 중…", "탭과 UI를 초기화하고 있습니다")

# ── 1: TXT변환 / 전체 실행 ───────────────────────────────
if _active_view in {"1_txt", "all_run"}:
    st.subheader("📄 TXT 변환")
    st.caption("텍스트로 저장 (OCR 변환된 문서만 가능) — PDF의 텍스트 레이어를 추출해 TXT로 저장합니다.")

    _ws1 = DEFAULT_WS
    _fast1 = True

    # 처리 모드
    _mode1 = st.radio(
        "처리 모드",
        ["📄 TXT저장만", "🚀 전체 실행 (TXT변환→장별분할→번역(영어문서인 경우)→Wiki)"],
        horizontal=True, key="ocr_mode",
    )

    # 번역 엔진 (전체 파이프라인 모드일 때만)
    _tr_eng1 = ""
    if "전체" in _mode1:
        _tr_opts1 = translate_engine_options()
        _tr_avail1 = [(eid, lbl) for eid, lbl, av, _ in _tr_opts1 if av]
        if _tr_avail1:
            _tr_lbl1 = st.radio("번역 엔진", [lbl for _, lbl in _tr_avail1],
                                 horizontal=True, key="ocr_tr_engine_radio")
            _tr_eng1 = next(eid for eid, lbl in _tr_avail1 if lbl == _tr_lbl1)
            _wiki_model_radio("ocr1_wiki_ai")

    # 파일 업로드
    _uploads1 = st.file_uploader(
        "PDF 또는 TXT 업로드 (여러 파일 가능)",
        type=["pdf", "txt", "md"], accept_multiple_files=True, key="ocr_uploader",
    )
    if _uploads1:
        _already_queued1 = st.session_state.get("_ocr_queued", set())
        _added1 = []
        for _uf_new in _uploads1:
            if _uf_new.name in _already_queued1:
                continue  # 이미 대기 목록에 추가된 파일 건너뜀
            _dest1 = UPLOAD_TMP / _uf_new.name
            try:
                _dest1.write_bytes(_uf_new.read())
                _added1.append(_uf_new.name)
                _already_queued1.add(_uf_new.name)
            except Exception as _e1:
                st.error(f"❌ 저장 실패: {_uf_new.name} — {_e1}")
        st.session_state["_ocr_queued"] = _already_queued1
        if _added1:
            st.success(f"📥 처리 대기 목록에 추가됨: {', '.join(_added1)}")
            st.rerun()  # 대기 목록 갱신 (세션스테이트로 중복 저장 방지됨)

    st.divider()

    # 처리 대기 목록 (UPLOAD_TMP)
    _pending_all1 = sorted(
        [f for f in UPLOAD_TMP.glob("*") if f.is_file() and f.suffix.lower() in {".pdf",".txt",".md"}]
        if UPLOAD_TMP.exists() else [],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    st.markdown(f"#### 처리 대기 ({len(_pending_all1)}개)")
    if _pending_all1:
        _items1 = [
            {"key": f.name,
             "label": f.name,
             "meta": f"{f.stat().st_size//1024}KB · {datetime.fromtimestamp(f.stat().st_mtime).strftime('%m-%d %H:%M')}",
             "obj": _PathAsUpload(f)}
            for f in _pending_all1
        ]
        _sel1 = _checklist(_items1, "ocr1", height=250)
        _b1c1, _b1c2 = st.columns(2)
        _run_sel1 = _b1c1.button(f"▶ 선택 처리 ({len(_sel1)}개)", key="ocr1_run_sel",
                                   use_container_width=True, type="primary", disabled=len(_sel1)==0)
        _run_all1 = _b1c2.button(f"▶ 전체 처리 ({len(_pending_all1)}개)", key="ocr1_run_all",
                                   use_container_width=True)
        _to_run1 = [_PathAsUpload(f) for f in _pending_all1] if _run_all1 else (_sel1 if _run_sel1 else [])
        if _to_run1:
            _prog1 = st.progress(0.0)
            for _i1, _uf1 in enumerate(_to_run1, 1):
                if "TXT저장" in _mode1:
                    with st.status(f"TXT변환 [{_i1}/{len(_to_run1)}]: {_uf1.name}", expanded=False):
                        _r1 = _do_ocr_only(_uf1, _ws1, fast=_fast1)
                    (st.success if _r1["ok"] else st.error)(
                        f"{'✅' if _r1['ok'] else '❌'} {_uf1.name}" +
                        (f" → `{Path(_r1['txt_path']).name}`" if _r1["ok"] else f": {_r1['error']}")
                    )
                else:
                    _ph1 = st.empty()
                    _process_file_for_pipeline(
                        _uf1, _ws1, _nfc(_ws1), True, _tr_eng1,
                        False, False, _ph1, do_wiki=False,
                    )
                _prog1.progress(_i1 / len(_to_run1))
            st.session_state.pop("_ocr_queued", None)  # 처리 완료 후 큐 초기화
            st.rerun()
    else:
        st.info("대기 중인 파일 없음 — 위에서 PDF를 업로드하세요.")

    st.divider()

    # 완료 기록
    _fws1 = DEFAULT_WS
    _done_txts1: list[Path] = []
    if _fws1 and DONE_DIR.exists():
        _t_sub1 = DONE_DIR / _fws1 / TXT_SUB
        if _t_sub1.exists():
            _done_txts1 = sorted(_t_sub1.glob("*.txt"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
    st.markdown(f"#### 완료 기록 ({len(_done_txts1)}권)")
    if _done_txts1:
        with st.container(height=220, border=True):
            for _dt1 in _done_txts1[:80]:
                _dc1, _dc2, _dc3 = st.columns([5, 2, 1])
                _dc1.caption(f"**{_dt1.stem}**")
                _dc2.caption(f"{_dt1.stat().st_size//1024}KB · "
                             f"{datetime.fromtimestamp(_dt1.stat().st_mtime).strftime('%m-%d')}")
                if _dc3.button("📂", key=f"open_dt1_{_dt1}", help="폴더에서 보기"):
                    open_path(_dt1, reveal=True)
    elif _fws1:
        st.caption("해당 폴더에 완료된 TXT 없음")

    # 실패 기록
    _fail1 = sorted([p for p in FAILED_DIR.rglob("*") if p.is_file()],
                    key=lambda p: p.stat().st_mtime, reverse=True) if FAILED_DIR.exists() else []
    if _fail1:
        with st.expander(f"⚠️ 실패 {len(_fail1)}건"):
            for _ff1 in _fail1[:30]:
                _fc1, _fc2, _fc3 = st.columns([5, 1, 1])
                _fc1.caption(_ff1.name)
                if _fc2.button("↩️", key=f"retry_f1_{_ff1}", help="재시도"):
                    shutil.move(str(_ff1), str(UPLOAD_TMP / _ff1.name)); st.rerun()
                if _fc3.button("🗑", key=f"del_f1_{_ff1}", help="삭제"):
                    try: _ff1.unlink()
                    except Exception: pass
                    st.rerun()

    st.info("💡 다음 단계: **📂 2·장별분할** 탭으로 이동하세요")


# ── 2: 장별 분할 ────────────────────────────────────────
if _active_view == "2_split":
    st.subheader("📂 장별 분할")
    st.caption("TXT를 장(Chapter) 단위로 분리해 챕터별 파일로 저장합니다.")

    # TXT 직접 업로드
    _up2 = st.file_uploader("TXT 직접 업로드 (done/ 폴더로 저장)",
                              type=["txt", "md"], accept_multiple_files=True, key="split_uploader")
    if _up2:
        for _u2 in _up2:
            txt_dir(DONE_DIR, DEFAULT_WS).mkdir(parents=True, exist_ok=True)
            _dst2 = txt_dir(DONE_DIR, DEFAULT_WS) / _u2.name
            _dst2.write_bytes(_u2.read())
        st.success(f"{len(_up2)}개 TXT 저장 완료"); st.rerun()

    # ── 분할 대기 (큐 기반 + 1_txt/ 전체 폴백) ──────────────
    _q2_stems = queue_list("tab2_ready")
    _split_pend2: list[dict] = []
    _split_done2: list[dict] = []
    _txt_root2 = DONE_DIR / DEFAULT_WS / TXT_SUB

    # 큐에 없어도 1_txt/에 있는 TXT 모두 포함
    _all_txt2_stems = {f.stem for f in _txt_root2.glob("*.txt")} if _txt_root2.exists() else set()
    _q2_stems_set = set(_q2_stems)
    _extra2 = sorted(_all_txt2_stems - _q2_stems_set)  # 큐에 없는 TXT
    _all2_stems = list(_q2_stems) + _extra2

    for _stem2 in _all2_stems:
        _txt2 = _txt_root2 / (_stem2 + ".txt")
        if not _txt2.exists():
            continue
        _ch2 = chapters_dir(DEFAULT_WS, _stem2)
        _ch_txts2 = [f for f in (_ch2.glob("??_*.txt") if _ch2.exists() else [])
                     if not f.stem.endswith(("_ko", "_wiki"))]
        _meta2 = f"{_txt2.stat().st_size//1024}KB" + ("" if _stem2 in _q2_stems_set else " ·미등록")
        if _ch_txts2:
            _split_done2.append({"stem": _stem2, "n": len(_ch_txts2), "ch_dir": _ch2})
        else:
            _split_pend2.append({"key": _stem2, "label": _stem2, "meta": _meta2,
                                  "obj": {"ws": DEFAULT_WS, "stem": _stem2}})

    st.markdown(f"#### 분할 대기 ({len(_split_pend2)}권)")
    if _split_pend2:
        _sel2 = _checklist(_split_pend2, "split2", height=280)
        _b2c1, _b2c2, _b2c3 = st.columns([2, 2, 1])
        _rs2 = _b2c1.button(f"▶ 선택 분할 ({len(_sel2)}권)", key="split2_run_sel",
                              use_container_width=True, type="primary", disabled=len(_sel2)==0)
        _ra2 = _b2c2.button(f"▶ 전체 분할 ({len(_split_pend2)}권)", key="split2_run_all",
                              use_container_width=True)
        if _b2c3.button("🗑 큐 비우기", key="split2_clear", use_container_width=True):
            queue_clear("tab2_ready"); st.rerun()
        _to2 = [it["obj"] for it in _split_pend2] if _ra2 else (_sel2 if _rs2 else [])
        if _to2:
            _sp2 = st.progress(0.0)
            for _si2, _s2 in enumerate(_to2, 1):
                with st.status(f"분할 [{_si2}/{len(_to2)}]: {_s2['stem']}", expanded=False):
                    _sn2, _serr2 = split_book_to_chapters(_s2["ws"], _s2["stem"])
                if _serr2:
                    st.warning(f"⚠️ {_s2['stem']}: {_serr2}")
                else:
                    st.success(f"✅ {_s2['stem']} → {_sn2}개 챕터")
                    queue_remove("tab2_ready", [_s2["stem"]])
                    _ch_dir2 = chapters_dir(_s2["ws"], _s2["stem"])
                    _new_chs2 = [str(f.relative_to(DONE_DIR))
                                 for f in sorted(_ch_dir2.glob("??_*.txt"))
                                 if not f.stem.endswith(("_ko", "_wiki"))]
                    if _new_chs2:
                        if _needs_translation(_s2["stem"]):
                            queue_add("tab3_ready", _new_chs2)
                            st.caption("🌐 영문 책 → 3·번역 대기 등록")
                        else:
                            queue_add("tab4_ready", _new_chs2)
                            st.caption("🇰🇷 한국어 책 → 번역 건너뜀, 4·요약 대기 등록")
                _sp2.progress(_si2 / len(_to2))
            st.rerun()
    else:
        st.info("분할 대기 없음 — 1·TXT변환 탭에서 TXT를 먼저 생성하거나 아래에서 수동 추가하세요")

    # 수동 추가 expander
    with st.expander("➕ 수동으로 추가 (기존 책에서 선택)"):
        _mc2a, _mc2b = st.columns([3, 2])
        _search2 = _mc2a.text_input("책 이름 검색", key="split2_search", placeholder="검색어 입력…")
        _sort2 = _mc2b.radio("정렬", ["최근 추가순", "이름순"], horizontal=True, key="split2_sort")
        _all_txts2 = list(_txt_root2.glob("*.txt")) if _txt_root2.exists() else []
        _all_txts2 = sorted(_all_txts2, key=lambda f: f.stat().st_mtime, reverse=True) \
                     if "최근" in _sort2 else sorted(_all_txts2, key=lambda f: f.name)
        _filtered2 = [f for f in _all_txts2 if _search2.lower() in f.stem.lower()] \
                     if _search2 else _all_txts2
        _manual_items2 = [{"key": f.stem, "label": f.stem,
                           "meta": f"{f.stat().st_size//1024}KB", "obj": f.stem}
                          for f in _filtered2]
        _msel2 = _checklist(_manual_items2, "split2m", height=220)
        if st.button(f"➕ 선택 항목 큐에 추가 ({len(_msel2)}권)", key="split2m_add",
                     disabled=len(_msel2)==0):
            queue_add("tab2_ready", _msel2); st.rerun()

    st.divider()
    st.markdown(f"#### 분할 완료 ({len(_split_done2)}권)")
    if _split_done2:
        with st.container(height=200, border=True):
            for _sd2 in _split_done2:
                _sdc1, _sdc2, _sdc3 = st.columns([5, 1.5, 1])
                _sdc1.markdown(f"**{_sd2['stem']}** &nbsp;<small style='color:#9ca3af'>{_sd2['n']}챕터</small>",
                               unsafe_allow_html=True)
                if _sdc2.button("📂 열기", key=f"open_ch2_{_sd2['stem']}", use_container_width=True):
                    open_path(_sd2["ch_dir"])
                if _sdc3.button("🔄", key=f"resplit2_{_sd2['stem']}", help="재분할"):
                    for _f2 in _sd2["ch_dir"].glob("*"):
                        try: _f2.unlink()
                        except Exception: pass
                    st.rerun()
    else:
        st.caption("완료된 분할 없음")

    st.info("💡 다음 단계: **🌐 3·번역** 탭으로 이동하세요")


# ── 3: 번역 ─────────────────────────────────────────────
if _active_view == "3_translate":
    st.subheader("🌐 영문 번역")
    st.caption("챕터 TXT를 하나씩 또는 일괄로 한국어 번역합니다.")

    _tr_opts3 = translate_engine_options()
    _tr_avail3 = [(eid, lbl) for eid, lbl, av, _ in _tr_opts3 if av]
    if not _tr_avail3:
        st.warning("번역 엔진 없음 — ⚙️ 설정 탭에서 API 키를 입력하세요.")
    else:
        _tr_lbl3 = st.radio("번역 엔진", [lbl for _, lbl in _tr_avail3],
                             horizontal=True, key="tr3_engine")
        _tr_eng3 = next(eid for eid, lbl in _tr_avail3 if lbl == _tr_lbl3)

        with st.expander("🔎 논문 출처로 가져와 번역", expanded=True):
            _paper_src3 = st.text_input(
                "논문 출처",
                key="tr3_paper_source",
                placeholder="URL, DOI(10.xxxx/...), doi:..., arXiv 번호 또는 arxiv.org 링크",
            )
            if st.button("다운로드 확인 후 번역", key="tr3_source_translate",
                         use_container_width=True, type="primary",
                         disabled=not _paper_src3.strip()):
                with st.status("논문 출처 확인 중…", expanded=True):
                    _ok_dl3, _src_file3, _reason3 = download_paper_source(_paper_src3)
                    if not _ok_dl3 or not _src_file3:
                        st.error(f"({_reason3}) 때문에 번역할 수 없습니다.")
                    else:
                        st.write(f"✅ 다운로드 가능: `{_src_file3.name}`")
                        _paper_prog3 = st.progress(0, text="번역 준비 중…")
                        def _paper_progress3(done, total, translated, preserved, dropped, failed):
                            _paper_prog3.progress(
                                min(done / max(total, 1), 1.0),
                                text=(
                                    f"번역 처리 중 {done}/{total} · 번역 {translated} · "
                                    f"원문보존 {preserved} · 삭제 {dropped}"
                                    + (f" · 실패보존 {failed}" if failed else "")
                                ),
                            )
                        _ok_tr3, _msg_tr3 = translate_downloaded_paper(
                            _src_file3, _tr_eng3, progress_cb=_paper_progress3
                        )
                        if _ok_tr3:
                            _paper_prog3.progress(1.0, text="번역 처리 완료")
                            st.success(f"✅ 번역 완료: {_msg_tr3}")
                        else:
                            st.error(f"({_msg_tr3}) 때문에 번역할 수 없습니다.")

        # TXT 직접 업로드 후 즉시 번역
        _up3 = st.file_uploader("TXT 직접 업로드 (즉시 번역)",
                                  type=["txt"], accept_multiple_files=True, key="tr3_uploader")
        if _up3:
            for _u3 in _up3:
                _tmp3 = Path(tempfile.gettempdir()) / _u3.name
                _tmp3.write_bytes(_u3.read())
                with st.status(f"번역 중: {_u3.name}", expanded=True):
                    _up_prog3 = st.progress(0, text="번역 준비 중…")
                    def _upload_progress3(done, total, translated, preserved, dropped, failed):
                        _up_prog3.progress(
                            min(done / max(total, 1), 1.0),
                            text=(
                                f"번역 처리 중 {done}/{total} · 번역 {translated} · "
                                f"원문보존 {preserved} · 삭제 {dropped}"
                                + (f" · 실패보존 {failed}" if failed else "")
                            ),
                        )
                    _ok3u, _msg3u = translate_one_chapter(
                        _tmp3, _tr_eng3, progress_cb=_upload_progress3
                    )
                    if _ok3u:
                        _up_prog3.progress(1.0, text="번역 처리 완료")
                (st.success if _ok3u else st.error)(f"{'✅' if _ok3u else '❌'} {_u3.name}: {_msg3u}")
            st.rerun()

        # ── 번역 대기 (큐 기반) ──────────────────────────────
        _q3_rels = queue_list("tab3_ready")   # Tab2가 등록한 챕터 경로(상대)
        _tr_pend3: list[dict] = []
        _tr_done3 = 0
        for _rel3 in _q3_rels:
            _cf3 = DONE_DIR / _rel3
            if not _cf3.exists():
                continue
            _ko3 = _cf3.with_name(_cf3.stem + "_ko.txt")
            if _ko3.exists():
                _tr_done3 += 1
            else:
                _tr_pend3.append({
                    "key": _rel3,
                    "label": f"{_cf3.parent.name} / {_cf3.name}",
                    "meta": f"{_cf3.stat().st_size//1024}KB",
                    "obj": _cf3,
                })

        st.divider()
        st.markdown(f"#### 번역 대기 ({len(_tr_pend3)}개) / 완료 {_tr_done3}개")
        if _tr_pend3:
            _sel3 = _checklist(_tr_pend3, "tr3", height=280)
            _b3c1, _b3c2, _b3c3 = st.columns([2, 2, 1])
            _rs3 = _b3c1.button(f"▶ 선택 번역 ({len(_sel3)}개)", key="tr3_run_sel",
                                  use_container_width=True, type="primary", disabled=len(_sel3)==0)
            _ra3 = _b3c2.button(f"▶ 전체 번역 ({len(_tr_pend3)}개)", key="tr3_run_all",
                                  use_container_width=True)
            if _b3c3.button("🗑 큐 비우기", key="tr3_clear", use_container_width=True):
                queue_clear("tab3_ready"); st.rerun()
            _to3: list = ([it["obj"] for it in _tr_pend3] if _ra3 else (_sel3 if _rs3 else []))
            if _to3:
                _tp3 = st.progress(0.0)
                for _ti3, _tf3 in enumerate(_to3, 1):
                    st.caption(f"번역 [{_ti3}/{len(_to3)}]: {_tf3.name}")
                    _ok3, _msg3 = translate_one_chapter(_tf3, _tr_eng3)
                    (st.success if _ok3 else st.warning)(
                        f"{'✅' if _ok3 else '⚠️'} {_tf3.name}: {_msg3}")
                    if _ok3:
                        _rel3_done = str(_tf3.relative_to(DONE_DIR))
                        queue_remove("tab3_ready", [_rel3_done])
                        queue_add("tab4_ready", [_rel3_done])
                    _tp3.progress(_ti3 / len(_to3))
                st.success(f"번역 처리 완료: {len(_to3)}개"); st.rerun()
        else:
            st.info("번역 대기 없음 — 2·장별분할 탭에서 챕터를 먼저 분리하세요")

        # 수동 추가 expander
        with st.expander("➕ 수동으로 추가 (기존 챕터에서 선택)"):
            _mc3a, _mc3b = st.columns([3, 2])
            _search3 = _mc3a.text_input("책/챕터 이름 검색", key="tr3_search", placeholder="검색어 입력…")
            _sort3 = _mc3b.radio("정렬", ["최근 추가순", "이름순"], horizontal=True, key="tr3_sort")
            _ch_root3m = DONE_DIR / DEFAULT_WS / "chapters"
            _all_cfs3 = list(_ch_root3m.rglob("??_*.txt")) if _ch_root3m.exists() else []
            _all_cfs3 = [f for f in _all_cfs3 if not f.stem.endswith(("_ko","_wiki"))]
            _all_cfs3 = sorted(_all_cfs3, key=lambda f: f.stat().st_mtime, reverse=True) \
                        if "최근" in _sort3 else sorted(_all_cfs3, key=lambda f: str(f))
            _filt3 = [f for f in _all_cfs3 if _search3.lower() in str(f).lower()] if _search3 else _all_cfs3
            _mitems3 = [{"key": str(f.relative_to(DONE_DIR)), "label": f"{f.parent.name}/{f.name}",
                         "meta": f"{f.stat().st_size//1024}KB", "obj": str(f.relative_to(DONE_DIR))}
                        for f in _filt3]
            _msel3 = _checklist(_mitems3, "tr3m", height=200)
            if st.button(f"➕ 선택 항목 큐에 추가 ({len(_msel3)}개)", key="tr3m_add", disabled=len(_msel3)==0):
                queue_add("tab3_ready", _msel3); st.rerun()

    st.info("💡 다음 단계: **📝 4·요약MD** 탭으로 이동하세요")


# ── 4: 요약MD ───────────────────────────────────────────
if _active_view == "4_summary":
    st.subheader("📝 요약MD 생성")
    st.caption("챕터 TXT(번역본 우선)로 Obsidian 노트용 요약 JSON을 생성합니다.")

    _prov_ok4 = any(llm.has_key(p) for p in llm.PROVIDERS)
    if not _prov_ok4:
        st.warning("요약 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요.")
    else:
        _wp4, _wm4 = _wiki_model_radio("summ4_ai")

        # TXT 직접 업로드
        _up4 = st.file_uploader("TXT 직접 업로드 (즉시 요약)",
                                  type=["txt"], accept_multiple_files=True, key="summ4_uploader")
        if _up4:
            for _u4 in _up4:
                _tmp4 = Path(tempfile.gettempdir()) / _u4.name
                _tmp4.write_bytes(_u4.read())
                _book4u = _nfc(_u4.name.split("_")[0]) if "_" in _u4.name else _nfc(_u4.name)
                with st.status(f"요약 중: {_u4.name}", expanded=True):
                    _ok4u, _msg4u = summarize_one_chapter(_tmp4, _book4u)
                (st.success if _ok4u else st.error)(f"{'✅' if _ok4u else '❌'} {_u4.name}: {_msg4u}")
            st.rerun()

        # ── 요약 대기 (큐 기반) ──────────────────────────────
        _q4_rels = queue_list("tab4_ready")
        _q4_failed_rels = queue_list("tab4_failed")
        _sum_pend4: list[dict] = []
        _sum_failed4: list[dict] = []
        _sum_done4 = 0
        _q4_remove_missing: list[str] = []
        _q4_remove_done: list[str] = []
        for _rel4 in _q4_rels:
            _cf4 = DONE_DIR / _rel4
            if not _cf4.exists():
                _q4_remove_missing.append(_rel4)
                continue
            _bstem4 = _nfc(_cf4.parent.name)
            _json4 = _cf4.with_name(_cf4.stem + "_wiki.json")
            if _json4.exists():
                _sum_done4 += 1
                _q4_remove_done.append(_rel4)
            else:
                _ko4 = _cf4.with_name(_cf4.stem + "_ko.txt")
                _tag4 = "🌐ko" if _ko4.exists() else "📄원문"
                _sum_pend4.append({
                    "key": _rel4,
                    "label": f"{_cf4.parent.name} / {_cf4.name}",
                    "meta": f"{_tag4} · {_cf4.stat().st_size//1024}KB",
                    "obj": (_cf4, _bstem4),
                })
        for _rel4f in _q4_failed_rels:
            _cf4f = DONE_DIR / _rel4f
            if not _cf4f.exists():
                _q4_remove_missing.append(_rel4f)
                continue
            _json4f = _cf4f.with_name(_cf4f.stem + "_wiki.json")
            if _json4f.exists():
                _sum_done4 += 1
                _q4_remove_done.append(_rel4f)
                continue
            _sum_failed4.append({
                "key": _rel4f,
                "label": f"{_cf4f.parent.name} / {_cf4f.name}",
                "meta": f"{_cf4f.stat().st_size//1024}KB · 실패",
                "obj": _rel4f,
            })
        if _q4_remove_missing:
            queue_remove("tab4_ready", _q4_remove_missing)
            queue_remove("tab4_failed", _q4_remove_missing)
        if _q4_remove_done:
            queue_remove("tab4_ready", _q4_remove_done)
            queue_remove("tab4_failed", _q4_remove_done)

        st.divider()
        st.markdown(f"#### 요약 대기 ({len(_sum_pend4)}개) / 완료 {_sum_done4}개")
        if _sum_pend4:
            _sel4 = _checklist(_sum_pend4, "summ4", height=280)
            _b4c1, _b4c2, _b4c3 = st.columns([2, 2, 1])
            _rs4 = _b4c1.button(f"▶ 선택 요약 ({len(_sel4)}개)", key="summ4_run_sel",
                                  use_container_width=True, type="primary", disabled=len(_sel4)==0)
            _ra4 = _b4c2.button(f"▶ 전체 요약 ({len(_sum_pend4)}개)", key="summ4_run_all",
                                  use_container_width=True)
            if _b4c3.button("🗑 큐 비우기", key="summ4_clear", use_container_width=True):
                queue_clear("tab4_ready"); st.rerun()
            _to4: list = ([it["obj"] for it in _sum_pend4] if _ra4 else (_sel4 if _rs4 else []))
            if _to4:
                _sp4 = st.progress(0.0)
                _stems4_done: set[str] = set()
                for _si4, (_sf4, _bst4) in enumerate(_to4, 1):
                    with st.status(f"요약 [{_si4}/{len(_to4)}]: {_sf4.name}", expanded=False):
                        _ok4, _msg4 = summarize_one_chapter(_sf4, _bst4)
                    (st.success if _ok4 else st.warning)(
                        f"{'✅' if _ok4 else '⚠️'} {_sf4.name}: {_msg4[:80]}")
                    if _ok4:
                        _rel4_done = str(_sf4.relative_to(DONE_DIR))
                        queue_remove("tab4_ready", [_rel4_done])
                        queue_remove("tab4_failed", [_rel4_done])
                        _stems4_done.add(_bst4)
                    else:
                        _rel4_fail = str(_sf4.relative_to(DONE_DIR))
                        queue_remove("tab4_ready", [_rel4_fail])
                        queue_add("tab4_failed", [_rel4_fail])
                    _sp4.progress(_si4 / len(_to4))
                # 책 단위로 모든 챕터 요약 완료된 것만 tab5 큐에 등록
                for _st5 in _stems4_done:
                    queue_add("tab5_ready", [_st5])
                st.success(f"요약 처리 완료: {len(_to4)}개"); st.rerun()
        else:
            st.info("요약 대기 없음 — 3·번역 탭 처리 후 자동 등록되거나 아래에서 수동 추가하세요")

        if _sum_failed4:
            st.markdown(f"#### 요약 실패 ({len(_sum_failed4)}개)")
            _fail_sel4 = _checklist(_sum_failed4, "summ4_failed", height=180)
            _f4c1, _f4c2 = st.columns([2, 1])
            if _f4c1.button(f"↻ 선택 재시도 대기 ({len(_fail_sel4)}개)", key="summ4_retry_failed",
                              use_container_width=True, disabled=len(_fail_sel4)==0):
                queue_remove("tab4_failed", _fail_sel4)
                queue_add("tab4_ready", _fail_sel4)
                st.rerun()
            if _f4c2.button("🗑 실패 목록 비우기", key="summ4_clear_failed", use_container_width=True):
                queue_clear("tab4_failed")
                st.rerun()

        # 수동 추가 expander
        with st.expander("➕ 수동으로 추가 (기존 챕터에서 선택)"):
            _mc4a, _mc4b = st.columns([3, 2])
            _search4 = _mc4a.text_input("책/챕터 이름 검색", key="summ4_search", placeholder="검색어 입력…")
            _sort4 = _mc4b.radio("정렬", ["최근 추가순", "이름순"], horizontal=True, key="summ4_sort")
            _ch_root4m = DONE_DIR / DEFAULT_WS / "chapters"
            _all_cfs4 = list(_ch_root4m.rglob("??_*.txt")) if _ch_root4m.exists() else []
            _all_cfs4 = [f for f in _all_cfs4 if not f.stem.endswith(("_ko","_wiki"))]
            _all_cfs4 = sorted(_all_cfs4, key=lambda f: f.stat().st_mtime, reverse=True) \
                        if "최근" in _sort4 else sorted(_all_cfs4, key=lambda f: str(f))
            _filt4 = [f for f in _all_cfs4 if _search4.lower() in str(f).lower()] if _search4 else _all_cfs4
            _mitems4 = [{"key": str(f.relative_to(DONE_DIR)), "label": f"{f.parent.name}/{f.name}",
                         "meta": f"{f.stat().st_size//1024}KB", "obj": str(f.relative_to(DONE_DIR))}
                        for f in _filt4]
            _msel4 = _checklist(_mitems4, "summ4m", height=200)
            if st.button(f"➕ 선택 항목 큐에 추가 ({len(_msel4)}개)", key="summ4m_add", disabled=len(_msel4)==0):
                queue_add("tab4_ready", _msel4); st.rerun()

    st.info("💡 다음 단계: **📖 5·Wiki반영** 탭으로 이동하세요")


# ── 5: Wiki반영 ─────────────────────────────────────────
if _active_view == "5_wiki":
    st.subheader("📖 Obsidian Wiki 반영")
    st.caption("챕터 요약(_wiki.json)들을 합쳐 Obsidian 노트로 생성합니다.")

    # ── 위키 저장 보관함(Vault) 선택 ──────────────────────────────────
    _vaults5 = list_obsidian_vaults()
    # 세션에 저장된 보관함(Vault) 경로가 있으면 우선 사용, 없으면 기본값
    _cur_wiki5 = st.session_state.get("wiki5_active_dir", str(WIKI_DIR))
    _cur_wiki5_path = Path(_cur_wiki5)
    with st.expander(f"📁 위키 저장 보관함(Vault): `{_cur_wiki5_path.name}`  (`{_cur_wiki5}`)", expanded=False):
        if _vaults5:
            _vault_opts5 = _vaults5 + ([] if _cur_wiki5 in _vaults5 else [_cur_wiki5])
            _vault_idx5 = _vault_opts5.index(_cur_wiki5) if _cur_wiki5 in _vault_opts5 else 0
            _vault_sel5 = st.selectbox("Obsidian 보관함(Vault) 선택", _vault_opts5, index=_vault_idx5,
                                       key="wiki5_vault_sel",
                                       format_func=lambda p: f"{Path(p).name}  ({p})")
            if _vault_sel5 != _cur_wiki5:
                if st.button("✅ 이 보관함(Vault)로 변경 (즉시 적용)", key="wiki5_vault_save"):
                    set_wiki_dir(_vault_sel5)
                    st.session_state["wiki5_active_dir"] = _vault_sel5
                    st.success(f"✅ 보관함(Vault) 변경됨: {_vault_sel5}")
                    st.rerun()
        else:
            st.info("Obsidian 보관함(Vault) 목록을 가져올 수 없습니다. Obsidian이 설치·실행됐는지 확인하세요.")
        _custom5 = st.text_input("또는 직접 경로 입력", key="wiki5_vault_custom", placeholder="/path/to/vault")
        if _custom5 and st.button("✅ 직접 입력 경로로 변경 (즉시 적용)", key="wiki5_vault_custom_save"):
            set_wiki_dir(_custom5)
            st.session_state["wiki5_active_dir"] = _custom5
            st.success(f"✅ 보관함(Vault) 변경됨: {_custom5}")
            st.rerun()

    _wiki_prov_ok5 = any(llm.has_key(p) for p in llm.PROVIDERS)
    if not _wiki_prov_ok5:
        st.warning("Wiki 생성 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요.")
    else:
        _wiki_model_radio("wiki5_ai")

    _fws5 = DEFAULT_WS
    _wiki_stems5 = {_nfc(p.stem) for p in WIKI_DIR.rglob("*.md")} if WIKI_DIR.exists() else set()

    # ── 챕터 요약 → Wiki (큐 기반) ───────────────────────────
    _q5_stems = queue_list("tab5_ready")   # Tab4가 등록한 책 stem
    _wiki_pend5: list[dict] = []
    _wiki_done5_list: list[dict] = []
    for _stem5 in _q5_stems:
        _ch5 = chapters_dir(DEFAULT_WS, _stem5)
        _jsons5 = list(_ch5.glob("*_wiki.json")) if _ch5.exists() else []
        _total5 = len([f for f in _ch5.glob("??_*.txt")
                       if not f.stem.endswith(("_ko", "_wiki"))]) if _ch5.exists() else 0
        _ratio5 = f"{len(_jsons5)}/{_total5}챕터"
        # 챕터 이름 목록 (NN_제목.txt → 제목)
        _ch_names5 = [_re.sub(r'^\d+_', '', f.stem) for f in sorted(_ch5.glob("??_*.txt"))
                      if not f.stem.endswith(("_ko","_wiki"))] if _ch5.exists() else []
        if _stem5 in _wiki_stems5:
            _wiki_done5_list.append({"stem": _stem5, "n": len(_jsons5), "total": _total5})
        else:
            _wiki_pend5.append({
                "key": _stem5,
                "label": _stem5,
                "meta": f"{_ratio5} 요약됨",
                "obj": {"ws": DEFAULT_WS, "stem": _stem5},
                "ch_names": _ch_names5,
            })

    # 챕터 요약 → Wiki
    st.markdown(f"#### 챕터 요약 → Wiki ({len(_wiki_pend5)}권 대기)")
    if _wiki_pend5:
        # 책 단위 체크리스트 + 챕터 이름 펼치기
        _sel5: list = []
        with st.container(height=320, border=True):
            _w5h1, _w5h2, _w5h3 = st.columns([0.05, 0.6, 0.35])
            _w5h2.markdown("**책 제목**", unsafe_allow_html=True)
            _w5h3.markdown("<small style='color:#9ca3af'>챕터</small>", unsafe_allow_html=True)
            for _it5 in _wiki_pend5:
                _k5 = f"wiki5_{_it5['key']}"
                _c5a, _c5b, _c5c = st.columns([0.05, 0.6, 0.35])
                _chk5 = _c5a.checkbox(" ", key=_k5, label_visibility="collapsed")
                if _chk5:
                    _sel5.append(_it5["obj"])
                _c5b.markdown(f"**{_it5['label']}**", unsafe_allow_html=True)
                _ch_preview5 = " · ".join(_it5["ch_names"][:4])
                if len(_it5["ch_names"]) > 4:
                    _ch_preview5 += f" … +{len(_it5['ch_names'])-4}개"
                _c5c.caption(_it5["meta"])
                if _it5["ch_names"]:
                    with st.expander(f"  ↳ {_ch_preview5}", expanded=False):
                        _ch5_dir = chapters_dir(DEFAULT_WS, _it5["obj"]["stem"])
                        for _cn5 in _it5["ch_names"]:
                            # NN_제목.txt → NN_제목_wiki.json 탐색
                            _cn5_txt = next((_ch5_dir.glob(f"??_{_cn5}.txt")), None) if _ch5_dir.exists() else None
                            _cn5_json = _cn5_txt.with_name(_cn5_txt.stem + "_wiki.json") if _cn5_txt else None
                            _has_json5 = bool(_cn5_json and _cn5_json.exists())
                            _cj1, _cj2 = st.columns([4, 1])
                            if _has_json5:
                                _cj1.markdown(f"✅ **{_cn5}**")
                                _safe_key5 = _re.sub(r"[^a-zA-Z0-9가-힣]", "_", _cn5)[:30]
                                if _cj2.button("Wiki", key=f"ch5w_{_it5['key'][:20]}_{_safe_key5}", use_container_width=True):
                                    _bok5, _bmsg5 = build_single_chapter_wiki(DEFAULT_WS, _it5["obj"]["stem"], _cn5_json, wiki_dir=_cur_wiki5_path)
                                    (st.success if _bok5 else st.error)(
                                        f"{'✅ ' + Path(_bmsg5).name if _bok5 else '❌ ' + _bmsg5}")
                                try:
                                    _pv5 = json.loads(_cn5_json.read_text(encoding="utf-8"))
                                    with st.expander(f"  📖 {_cn5[:35]}", expanded=False):
                                        if _pv5.get("summary"):
                                            st.info(_pv5["summary"])
                                        if _pv5.get("body"):
                                            st.markdown(_pv5["body"])
                                except Exception:
                                    pass
                            else:
                                _cj1.caption(f"⏳ {_cn5}")
        _b5c1, _b5c2, _b5c3 = st.columns([2, 2, 1])
        _rs5 = _b5c1.button(f"▶ 선택 Wiki생성 ({len(_sel5)}권)", key="wiki5_run_sel",
                              use_container_width=True, type="primary", disabled=len(_sel5)==0)
        _ra5 = _b5c2.button(f"▶ 전체 Wiki생성 ({len(_wiki_pend5)}권)", key="wiki5_run_all",
                              use_container_width=True)
        if _b5c3.button("🗑 큐 비우기", key="wiki5_clear", use_container_width=True):
            queue_clear("tab5_ready"); st.rerun()
        _to5 = ([it["obj"] for it in _wiki_pend5] if _ra5 else (_sel5 if _rs5 else []))
        if _to5:
            _wp5 = st.progress(0.0)
            for _wi5, _wo5 in enumerate(_to5, 1):
                with st.status(f"Wiki [{_wi5}/{len(_to5)}]: {_wo5['stem']}", expanded=True):
                    # 1단계: 챕터별 개별 노트 생성
                    _ch_dir5 = chapters_dir(_wo5["ws"], _wo5["stem"])
                    _ch_jsons5 = sorted(_ch_dir5.glob("*_wiki.json")) if _ch_dir5.exists() else []
                    _ch_ok5, _ch_fail5 = 0, 0
                    for _cjf5 in _ch_jsons5:
                        _bok5, _bmsg5 = build_single_chapter_wiki(
                            _wo5["ws"], _wo5["stem"], _cjf5, wiki_dir=_cur_wiki5_path)
                        if _bok5:
                            _ch_ok5 += 1
                        else:
                            _ch_fail5 += 1
                            st.warning(f"챕터 노트 실패: {_cjf5.stem} — {_bmsg5}")
                    if _ch_jsons5:
                        st.write(f"챕터 노트: ✅ {_ch_ok5}개 생성" + (f", ❌ {_ch_fail5}개 실패" if _ch_fail5 else ""))
                    # 2단계: 전체 요약 노트 생성 (챕터 노트 완료 후)
                    _ok5, _msg5 = build_wiki_from_chapter_summaries(_wo5["ws"], _wo5["stem"], wiki_dir=_cur_wiki5_path)
                (st.success if _ok5 else st.error)(
                    f"{'✅' if _ok5 else '❌'} {_wo5['stem']}: "
                    f"{Path(_msg5).name if _ok5 else _msg5}")
                if _ok5:
                    queue_remove("tab5_ready", [_wo5["stem"]])
                _wp5.progress(_wi5 / len(_to5))
            st.rerun()
    else:
        st.info("Wiki 대기 없음 — 4·요약MD 탭에서 요약 완료 후 자동 등록되거나 아래에서 수동 추가하세요")

    # 수동 추가 expander (책 단위)
    with st.expander("➕ 수동으로 추가 (요약 완료된 책에서 선택)"):
        _mc5a, _mc5b = st.columns([3, 2])
        _search5 = _mc5a.text_input("책 이름 검색", key="wiki5_search", placeholder="검색어 입력…")
        _sort5 = _mc5b.radio("정렬", ["최근 추가순", "이름순"], horizontal=True, key="wiki5_sort")
        _ch_root5m = DONE_DIR / DEFAULT_WS / "chapters"
        _all_books5 = list(_ch_root5m.iterdir()) if _ch_root5m.exists() else []
        _books_with_json5 = [d for d in _all_books5 if d.is_dir() and list(d.glob("*_wiki.json"))]
        _books_with_json5 = sorted(_books_with_json5, key=lambda d: d.stat().st_mtime, reverse=True) \
                            if "최근" in _sort5 else sorted(_books_with_json5, key=lambda d: d.name)
        _filt5 = [d for d in _books_with_json5 if _search5.lower() in d.name.lower()] if _search5 else _books_with_json5
        _mitems5 = [{"key": d.name, "label": d.name,
                     "meta": f"{len(list(d.glob('*_wiki.json')))}챕터 요약", "obj": d.name}
                    for d in _filt5]
        _msel5 = _checklist(_mitems5, "wiki5m", height=200)
        if st.button(f"➕ 선택 항목 큐에 추가 ({len(_msel5)}권)", key="wiki5m_add", disabled=len(_msel5)==0):
            queue_add("tab5_ready", _msel5); st.rerun()

    # 단일 TXT 기반 (챕터 분할 없는 책 — 큐 외 별도 경로)
    _single_pend5: list[dict] = []
    _t5s = DONE_DIR / DEFAULT_WS / TXT_SUB
    if _t5s.exists():
        for _txt5s in sorted(_t5s.glob("*.txt")):
            _stem5s = _nfc(_txt5s.stem)
            _ch5s = chapters_dir(DEFAULT_WS, _stem5s)
            if _ch5s.exists() and any(f for f in _ch5s.glob("??_*.txt")
                                       if not f.stem.endswith(("_ko","_wiki"))):
                continue
            if _stem5s in _wiki_stems5:
                continue
            _single_pend5.append({
                "key": f"s_{_stem5s}",
                "label": _stem5s,
                "meta": f"단일TXT · {_txt5s.stat().st_size//1024}KB",
                "obj": {"ws": DEFAULT_WS, "stem": _stem5s, "txt": _txt5s},
            })

    # 단일 TXT → Wiki (Gemini 직접)
    if _single_pend5:
        st.divider()
        st.markdown(f"#### 단일 TXT → Wiki ({len(_single_pend5)}권 · 챕터 분할 없음)")
        st.caption("전체 TXT를 Gemini에 넣어 백그라운드로 단일 위키 노트 생성")
        _sel5s = _checklist(_single_pend5, "wiki5s", height=200)
        if st.button(f"▶ 선택 단일 Wiki ({len(_sel5s)}권)", key="wiki5s_run",
                     use_container_width=True, type="primary", disabled=len(_sel5s)==0):
            for _wo5s in _sel5s:
                _ok5s = trigger_gemini_wiki(_wo5s["txt"])
                (st.success if _ok5s else st.error)(
                    f"{'✅ 백그라운드 시작' if _ok5s else '❌ 실패'}: {_wo5s['stem']}")
            st.rerun()

    # Wiki 완료 목록
    st.divider()
    _wiki_files5 = sorted(WIKI_DIR.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) \
                   if WIKI_DIR.exists() else []
    st.markdown(f"#### Wiki 완료 ({len(_wiki_files5)}노트)")
    if _wiki_files5:
        _wv_col1, _wv_col2 = st.columns(2)
        if _wv_col1.button("📓 Obsidian 보관함(Vault) 열기", key="w5_vault", use_container_width=True):
            open_wiki_vault()
        if _wv_col2.button("📂 폴더 열기", key="w5_folder", use_container_width=True):
            open_path(WIKI_DIR)
        with st.container(height=300, border=True):
            for _wf5 in _wiki_files5[:100]:
                _wc1, _wc2, _wc3 = st.columns([5, 2, 1])
                _wc1.caption(f"**{_wf5.stem}**")
                _wc2.caption(datetime.fromtimestamp(_wf5.stat().st_mtime).strftime("%m-%d %H:%M"))
                if _wc3.button("📂", key=f"w5_open_{_wf5}", help="열기"):
                    open_path(_wf5)
    else:
        st.caption("생성된 Wiki 없음")


# ── 설정 (API 키) ─────────────────────────────────────
if _active_view == "settings":
    st.subheader("⚙️ API 키 설정")
    st.caption(
        "앱에 저장한 키를 우선 사용하고, 없으면 이 컴퓨터의 환경변수에서 감지된 키를 사용합니다. "
        "저장 키는 `~/.config/mybookshelf/keys.json`에만 보관되며 저장소에 올라가지 않습니다."
    )

    # 🧠 위키 생성 모델 (공급자/모델)
    _wp, _wm = llm.wiki_provider_model()
    st.markdown(f"**🧠 위키 생성 모델** — 현재: `{_wp} · {_wm}`")
    _avail = [(p, m) for p, info in llm.PROVIDERS.items() if llm.has_key(p) for m in info["models"]]
    if _avail:
        _labels = [f"{llm.PROVIDERS[p]['label']} · {m}" for p, m in _avail]
        _curlbl = f"{llm.PROVIDERS.get(_wp, {}).get('label', _wp)} · {_wm}"
        _idx = _labels.index(_curlbl) if _curlbl in _labels else 0
        _sel = st.selectbox("위키 노트를 생성할 모델", _labels, index=_idx, key="wiki_model_sel")
        _p, _m = _avail[_labels.index(_sel)]
        if (_p, _m) != (_wp, _wm) and st.button("✅ 이 모델로 위키 생성", use_container_width=True):
            llm.set_wiki_model(_p, _m); st.success(f"위키 모델 = {_p} · {_m}"); st.rerun()
    else:
        st.info("키 등록된 공급자가 없어 Gemini 기본값을 씁니다. 아래에서 키를 입력하세요.")
    st.caption("번역과 별개로, 위키 노트 생성에 쓸 모델입니다. 구조화 출력은 공급자별로 자동 처리됩니다.")
    st.divider()

    # API 키 입력 (CLI 공급자 제외)
    _cli_provs = {"claude_cli", "codex_cli"}
    for _prov, _info in llm.PROVIDERS.items():
        if _prov in _cli_provs:
            continue
        _cur = llm.masked(_prov)
        _src = llm.key_source(_prov)
        _src_label = {"saved": "저장됨", "detected": "감지됨"}.get(_src, "미설정")
        with st.expander(f"{_info['label']}  —  {('✅ ' + _src_label + ' ' + _cur) if _cur else '미설정'}",
                         expanded=not bool(_cur)):
            with st.form(f"keyform_{_prov}", clear_on_submit=True):
                _newk = st.text_input(f"{_info['label']} API 키", type="password",
                                      placeholder=_info["hint"], key=f"keyin_{_prov}")
                _c1, _c2 = st.columns(2)
                _save = _c1.form_submit_button("💾 저장", use_container_width=True)
                _del = _c2.form_submit_button("🗑 삭제", use_container_width=True)
                if _save:
                    if _newk.strip():
                        llm.save_key(_prov, _newk.strip())
                        st.success("저장됨")
                        st.rerun()
                    else:
                        st.warning("키를 입력하세요.")
                if _del:
                    llm.save_key(_prov, "")
                    st.info("저장 키 삭제됨. 환경변수 키가 있으면 계속 감지됩니다.")
                    st.rerun()
            if _src == "saved":
                st.caption("현재 앱 설정에 저장된 키를 사용합니다.")
            elif _src == "detected":
                _envs = ", ".join(llm.ENV_KEY_NAMES.get(_prov, ()))
                st.caption(f"현재 환경변수에서 감지된 키를 사용합니다: `{_envs}`")
            st.caption(f"모델: {', '.join(_info['models'])}")
    st.divider()
    st.markdown("**🖥 CLI 구독 도구** — API 키 없이 구독으로 사용")
    _cc1, _cc2 = st.columns(2)
    with _cc1:
        st.markdown("**Claude CLI**")
        _claude_installed = llm.claude_cli_installed()
        _claude_enabled = bool(llm.get_pref("use_claude_cli", False))
        if _claude_installed:
            st.success(f"설치됨\n`{llm.claude_cli_path()}`")
            _new_enabled_label = st.radio(
                "Claude 구독 CLI",
                ["비활성", "활성"],
                index=1 if _claude_enabled else 0,
                key="set_use_claude_cli",
                horizontal=True,
                help="Claude 구독을 사용 중이고 CLI 로그인이 되어 있을 때만 켜세요.",
            )
            _new_enabled = _new_enabled_label == "활성"
            if _new_enabled != _claude_enabled:
                llm.set_claude_cli_enabled(_new_enabled)
                st.rerun()
            if not _claude_enabled:
                st.caption("현재 비활성화됨 — API 키 방식 Claude와는 별개입니다.")
        else:
            st.info("미설치. `npm install -g @anthropic-ai/claude-code`")
    with _cc2:
        st.markdown("**Codex CLI**")
        _codex_installed = llm.codex_cli_installed()
        _codex_enabled = bool(llm.get_pref("use_codex_cli", False))
        if _codex_installed:
            st.success(f"설치됨\n`{llm.codex_cli_path()}`")
            _new_codex_enabled_label = st.radio(
                "Codex CLI",
                ["비활성", "활성"],
                index=1 if _codex_enabled else 0,
                key="set_use_codex_cli",
                horizontal=True,
                help="ChatGPT 계정 또는 API 키로 Codex CLI 로그인이 되어 있을 때만 켜세요.",
            )
            _new_codex_enabled = _new_codex_enabled_label == "활성"
            if _new_codex_enabled != _codex_enabled:
                llm.set_codex_cli_enabled(_new_codex_enabled)
                st.rerun()
            if not _codex_enabled:
                st.caption("현재 비활성화됨 — OpenAI API 키 방식과는 별개입니다.")
        else:
            st.info("미설치. `npm install -g @openai/codex`")

    st.divider()
    st.subheader("📓 위키 저장 폴더 (옵시디언 보관함(Vault))")
    st.caption(
        f"현재: `{WIKI_DIR}` — 생성된 위키 노트가 여기 저장되고, "
        "Wiki 목록 탭의 [옵시디언에서 위키 보관함(Vault) 열기]도 이 폴더를 엽니다."
    )
    _default_wiki = str(cfg.BASE_DIR / "wiki")
    _wiki_cands: list[str] = []
    for _c in [_default_wiki] + list_obsidian_vaults():
        if _c and _c not in _wiki_cands:
            _wiki_cands.append(_c)
    _cur_wiki = str(WIKI_DIR)
    _wd_sel = st.selectbox(
        "폴더 선택 — 기본값 + 옵시디언에 등록된 보관함(Vault)들",
        _wiki_cands,
        index=_wiki_cands.index(_cur_wiki) if _cur_wiki in _wiki_cands else 0,
        key="wiki_dir_sel",
    )
    _wd_custom = st.text_input("또는 폴더 경로 직접 입력 (비우면 위 선택 사용)", value="", key="wiki_dir_custom")
    _wd_target = (_wd_custom.strip() or _wd_sel).strip()
    if st.button("💾 위키 보관함(Vault) 저장 (즉시 적용)", use_container_width=True, key="wiki_dir_save"):
        if _wd_target == _cur_wiki:
            st.info("이미 이 폴더를 쓰고 있습니다.")
        else:
            set_wiki_dir(_wd_target)
            st.session_state["wiki5_active_dir"] = _wd_target
            st.success(f"✅ 저장됨: `{_wd_target}` — Tab 5에 즉시 반영됩니다")
    st.caption("ℹ️ 기존에 만든 노트는 자동으로 옮겨지지 않습니다. 옮기려면 폴더에서 직접 이동하세요.")

# 로딩 오버레이 제거 + 이후 재렌더링에서는 오버레이 건너뜀
_loading_ph.empty()
st.session_state["_app_loaded"] = True
