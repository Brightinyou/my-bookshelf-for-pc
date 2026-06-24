#!/usr/bin/env python3
"""My Bookshelf — PDF→Wiki 파이프라인 (Streamlit GUI)"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import config as cfg
import llm_providers as llm

# ── 설정 ─────────────────────────────────────────────────
# 기계 의존 값(경로·바이너리·분류 폴더)은 전부 config.py가 해석한다.
# 기본값 ~/Documents/My Bookshelf, 덮어쓰기 ~/.config/mybookshelf/config.json.
APP_VERSION = "v0.4.5"   # 배포 zip 버전과 함께 올린다
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")

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
#   1_txt(②변환 TXT, Gemini 입력) → 2_md(③Docling MD, 각주·표) → 3_translated(④번역)
TXT_SUB   = "1_txt"
MD_SUB    = "2_md"
TRANS_SUB = "3_translated"
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

def pdf_to_txt(pdf_path: Path) -> tuple[Path | None, Path | None, str]:
    """(txt_path, md_path, error_msg) 반환. md_path는 MD 생성 성공 시에만 채워짐.
    Docling(레이아웃 인식 + ocrmac OCR)이 기본, 없으면 pdftotext(텍스트 레이어) 폴백.
    동시에 깨끗한 .md 사이드카 파일을 PDF 폴더에 생성."""
    pdftotext = cfg.PDFTOTEXT

    txt_path = Path(tempfile.gettempdir()) / (pdf_path.stem + ".txt")

    # ── Docling 변환 (2026-06-09): 레이아웃 인식으로 본문/각주/표 분리 + ocrmac(Apple Vision) OCR ──
    docling_bin = Path(cfg.DOCLING) if cfg.DOCLING else None
    md_path_out: Path | None = None

    if docling_bin and docling_bin.exists():
        st.caption("📄 Docling 변환 중 — 레이아웃 인식·각주 분리 (대형 스캔은 수 분 소요)…")
        out_dir = pdf_path.parent
        # OS별 OCR 엔진: 맥=ocrmac(Apple Vision), 그 외=easyocr(ko 지원).
        # 언어 미지정 시 영어 기본 → 한글 깨짐. rapidocr은 docling이 중국어·영어 모델만
        # 지원해 한국어 불가(2026-06-10 확인) — easyocr만이 윈도우 한글 경로.
        # OCR 언어는 설정탭에서 변경 가능 (2026-06-13 다국어) — 태국어 등 추가 시
        # 맥(Vision)=th-TH 형식, 윈도우(EasyOCR)=th 형식.
        if sys.platform == "darwin":
            _ocr_langs = (llm.get_pref("ocr_langs_mac") or "ko-KR,en-US").strip()
            ocr_args = ["--ocr-engine", "ocrmac", "--ocr-lang", _ocr_langs]
        else:
            # Windows: 스캔 PDF는 WinRT→Tesseract 라우터, 디지털 PDF는 EasyOCR via Docling.
            # 기본 PDF 백엔드(dlparse)는 윈도우 한글 파일명·std::bad_alloc 크래시
            # → pypdfium2 백엔드 강제 (2026-06-11 실기 확인).
            _ocr_langs = (llm.get_pref("ocr_langs_other") or "ko,en").strip()
            _lang_code  = target_lang()
            # 스캔 여부 감지 → WinRT/Tesseract 라우터 시도
            try:
                from ocr_windows import is_scanned, ocr_windows_scanned
                if is_scanned(pdf_path, pdftotext):
                    st.caption("🔍 스캔 PDF 감지 — WinRT/Tesseract OCR 시도 중…")
                    _win_text, _win_err = ocr_windows_scanned(
                        pdf_path, _lang_code, str(docling_bin), _ocr_langs
                    )
                    if _win_text:
                        txt_path.write_text(_win_text, encoding="utf-8")
                        return txt_path, None, ""
                    elif _win_err and "EasyOCR 폴백" not in _win_err:
                        st.caption(f"⚠️ WinRT/Tesseract 실패 ({_win_err[:80]}) — EasyOCR로 폴백")
            except Exception as _we:
                st.caption(f"⚠️ OCR 라우터 오류 ({type(_we).__name__}) — EasyOCR로 폴백")
            ocr_args = ["--ocr-engine", "easyocr", "--ocr-lang", _ocr_langs,
                        "--pdf-backend", "pypdfium2"]
        # 이전 실행이 남긴 같은 이름 MD가 있으면 제거 — 변환 실패를 잔재가
        # 성공으로 가리는 것 방지 (2026-06-11, 0바이트 PDF '완료' 오판 원인)
        _stale = out_dir / (pdf_path.stem + ".md")
        if _stale.exists():
            _stale.unlink()
        try:
            r = subprocess.run(
                [str(docling_bin), str(pdf_path), "--to", "md",
                 "--image-export-mode", "placeholder",
                 *ocr_args,
                 "--output", str(out_dir)],
                capture_output=True, text=True, timeout=3600,
            )
        except subprocess.TimeoutExpired:
            return None, None, "Docling 변환 타임아웃(3600초) — 초대형 스캔 PDF"
        except Exception as e:
            return None, None, f"Docling 실행 오류: {type(e).__name__} {str(e)[:200]}"
        cand = out_dir / (pdf_path.stem + ".md")
        if not (cand.exists() and cand.stat().st_size > 0):
            return None, None, f"Docling 변환 실패 (exit {r.returncode}): {(r.stderr or '')[-300:]}"
        md_path_out = cand
        # TXT = MD 본문(이미지 placeholder 제거) — 번역·Gemini 위키용
        _md = cand.read_text(encoding="utf-8", errors="ignore")
        _md = _re.sub(r"!\[Image\]\([^)]*\)\s*", "", _md)
        txt_path.write_text(_md, encoding="utf-8")
    else:
        # 폴백: pdftotext (텍스트 레이어만)
        if not pdftotext or not Path(pdftotext).exists():
            return None, None, "docling·pdftotext 둘 다 없음 — 설정 또는 설치 필요."
        r = subprocess.run([pdftotext, str(pdf_path), str(txt_path)], capture_output=True, text=True)
        if r.returncode != 0:
            return None, None, f"pdftotext 오류 (exit {r.returncode}): {(r.stderr or '').strip() or '알 수 없는 오류'}"

    if not txt_path.exists() or txt_path.stat().st_size == 0:
        return None, None, "텍스트 추출 실패 (PDF 손상 또는 빈 PDF)"

    return txt_path, md_path_out, ""


# ── 번역: 영어→한국어 고정 ────────────────────────────────
_KO_SCRIPT = _re.compile(r"[가-힣]")


def target_lang() -> str:
    return "ko"


def needs_translation(txt_path: Path, threshold: float = 0.3) -> bool:
    """한글 비율이 낮으면 번역 필요로 판단."""
    sample = txt_path.read_text(encoding="utf-8", errors="ignore")[:3000]
    ko_ratio = len(_KO_SCRIPT.findall(sample)) / max(len(sample), 1)
    return ko_ratio < threshold


def is_english(txt_path: Path, threshold: float = 0.3) -> bool:
    return needs_translation(txt_path, threshold)


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
    """위키 생성기 자식 프로세스 환경. 업로드 탭에서 고른 금고가 있으면
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
                   + (f" → 금고 {env['MYBOOKSHELF_WIKI_DIR']}" if "MYBOOKSHELF_WIKI_DIR" in env else ""))
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
                   + (f" → 금고 {env['MYBOOKSHELF_WIKI_DIR']}" if "MYBOOKSHELF_WIKI_DIR" in env else ""))
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
        # PDF → DONE
        done_sub = DONE_DIR / ws_name
        done_sub.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            final_pdf = done_sub / uf.name
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
    """folder를 옵시디언 금고 목록에 등록(이미 있으면 그대로). (2026-06-11)"""
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
        append_log(f"WARN: 옵시디언 금고 등록 실패 ({type(e).__name__}) {str(e)[:120]}")
        return False


def list_obsidian_vaults() -> list[str]:
    """옵시디언에 등록된 금고 경로 목록. (2026-06-11)"""
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


def open_wiki_vault():
    """위키 폴더를 옵시디언 금고로 등록 후 옵시디언으로 열기. 실패 시 폴더라도 연다."""
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


def translate_one_chapter(ch_path: Path, engine: str) -> tuple[bool, str]:
    """단일 챕터 TXT 번역 → _ko.txt 저장. (ok, msg)."""
    try:
        text = ch_path.read_text(encoding="utf-8", errors="ignore")
        ko_path = ch_path.with_name(ch_path.stem + "_ko.txt")
        if not needs_translation(ch_path):
            ko_path.write_text(text, encoding="utf-8")
            return True, "이미 한국어 — 그대로 복사"
        paras = _split_paragraphs_robust(text)
        out: list[str] = []
        for p in paras:
            if should_drop_paragraph(p):
                continue
            if should_skip_translation(p):
                out.append(p)
            else:
                ko = translate(p, engine)
                out.append(ko if ko else p)
        ko_path.write_text("\n\n".join(out), encoding="utf-8")
        return True, f"{len(out)}단락 번역 완료"
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
        (ch_path.with_name(ch_path.stem + "_wiki.json")).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True, (data.get("summary") or "")[:120]
    except Exception as e:
        return False, str(e)[:200]


def build_wiki_from_chapter_summaries(ws_name: str, stem: str) -> tuple[bool, str]:
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
    lines = [
        "---", f"title: {stem}", f"category: {cat}",
        f"model: {model}", f"generated: {today}", "---", "",
        f"# {stem}", "", intro, "", f"**요약:** {summ}", "",
    ]
    for s in sections:
        lines += [f"## {s['idx']:02d}. {s['title']}", s["summary"], "", s["body"], ""]
    out_path = WIKI_DIR / _gw.make_filename(_gw.nfc(stem))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    _gw.mark_done(_gw.nfc(stem + ".txt"))
    append_log(f"단계별 Wiki 생성 완료: {out_path.name}")
    return True, str(out_path)


# ─── OCR 단독 처리 (번역·위키 생략) ─────────────────────────

def _do_ocr_only(uf, ws_name: str) -> dict:
    """PDF → TXT 변환만 수행. 번역·위키 생략. {ok, name, txt_path, md_path, error}"""
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
        return {"ok": True, "name": uf.name, "txt_path": str(final), "md_path": "", "error": ""}
    txt_path, md_src, err = pdf_to_txt(dest)
    if not txt_path:
        try: shutil.move(str(dest), str(FAILED_DIR / uf.name))
        except Exception: pass
        append_log(f"ERROR: OCR 실패 — {uf.name}: {err}")
        return {"ok": False, "name": uf.name, "txt_path": "", "md_path": "", "error": err}
    final_pdf = done_sub / uf.name
    shutil.move(str(dest), str(final_pdf))
    if md_src and md_src.exists():
        txt_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        md_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
        final_txt = txt_dir(DONE_DIR, ws_name) / txt_path.name
        final_md  = md_dir(DONE_DIR, ws_name) / md_src.name
        shutil.move(str(txt_path), str(final_txt))
        shutil.move(str(md_src),   str(final_md))
    else:
        final_txt = done_sub / txt_path.name
        shutil.move(str(txt_path), str(final_txt))
        final_md = None
    append_log(f"OCR 완료: {uf.name} → {Path(final_txt).name}")
    return {"ok": True, "name": uf.name, "txt_path": str(final_txt),
            "md_path": str(final_md) if final_md else "", "error": ""}


# ── UI ────────────────────────────────────────────────────

st.set_page_config(page_title="My Bookshelf", page_icon="📚", layout="wide")

# ── 글로벌 스타일 (2026-05-18 v2 — Linear·Vercel 톤) ────────────
# 잔잔한 segmented control + 모노톤 칩. 선택된 것만 도드라지는 미감.
st.markdown("""
<style>
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
</style>
""", unsafe_allow_html=True)

st.markdown(
    f"# 📚 My Bookshelf <span style='font-size:0.42em;color:#9aa0a6;"
    f"font-weight:400;vertical-align:middle'>{APP_VERSION}</span>",
    unsafe_allow_html=True,
)
st.caption("PDF → OCR/TXT → 장별 분할 → 번역 → 요약 → Obsidian Wiki")

# ── 상태 배너 ────────────────────────────────────────────
_avail_providers = [info["label"] for prov, info in llm.PROVIDERS.items() if llm.has_key(prov)]
_wiki_key_ok = any(llm.has_key(p) for p in llm.PROVIDERS)
wg_ok = wiki_generator_running()
col_s1, col_s2, col_s3 = st.columns(3)
col_s1.metric("API 키", f"{len(_avail_providers)}개" if _avail_providers else "❌ 없음")
col_s2.metric("위키 생성기", "🔄 생성 중" if wg_ok else "대기")
col_s3.metric("Wiki 완성", sum(1 for _ in WIKI_DIR.rglob("*.md")))
if not _avail_providers:
    st.error("⚠️ 사용 가능한 API가 없습니다 — ⚙️ 설정 탭에서 키를 입력하세요.")

# ── 탭 6개 ────────────────────────────────────────────────
tab_ocr, tab_split, tab_tr, tab_summ, tab_wiki5, tab_settings = st.tabs([
    "📄 1·OCR/TXT",
    "📂 2·장별분할",
    "🌐 3·번역",
    "📝 4·요약MD",
    "📖 5·Wiki반영",
    "⚙️ 설정",
])



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
            chk = c1.checkbox("", key=k, label_visibility="collapsed")
            c2.markdown(
                f"**{it['label']}** &nbsp;<small style='color:#9ca3af'>{it['meta']}</small>",
                unsafe_allow_html=True,
            )
            if chk:
                selected.append(it["obj"])
    return selected


# ── 탭1: OCR/TXT제작 ──────────────────────────────────────
with tab_ocr:
    st.subheader("📄 OCR/TXT 제작")
    st.caption("PDF를 업로드하면 OCR(텍스트 추출)하여 TXT 파일로 저장합니다.")

    _ws1 = DEFAULT_WS
    # 처리 모드
    _mode1 = st.radio(
        "처리 모드",
        ["📄 OCR만 (TXT저장)", "🚀 전체 파이프라인 (OCR→번역→Wiki)"],
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

    # 파일 업로드
    _uploads1 = st.file_uploader(
        "PDF 또는 TXT 업로드 (여러 파일 가능)",
        type=["pdf", "txt", "md"], accept_multiple_files=True, key="ocr_uploader",
    )
    if _uploads1:
        for _uf_new in _uploads1:
            with st.status(f"처리 중: {_uf_new.name}", expanded=True):
                if "OCR만" in _mode1:
                    _r_new = _do_ocr_only(_uf_new, _ws1)
                    if _r_new["ok"]:
                        st.success(f"✅ TXT 저장: `{Path(_r_new['txt_path']).name}`")
                        if st.button("📂 결과 폴더 열기", key=f"open_ocr_{_uf_new.name}"):
                            open_path(Path(_r_new["txt_path"]), reveal=True)
                    else:
                        st.error(f"❌ {_r_new['error']}")
                else:
                    _ph_new = st.empty()
                    _process_file_for_pipeline(
                        _uf_new, _ws1, _nfc(_ws1), True, _tr_eng1,
                        False, False, _ph_new, do_wiki=True,
                    )
        st.rerun()

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
                if "OCR만" in _mode1:
                    with st.status(f"OCR [{_i1}/{len(_to_run1)}]: {_uf1.name}", expanded=False):
                        _r1 = _do_ocr_only(_uf1, _ws1)
                    (st.success if _r1["ok"] else st.error)(
                        f"{'✅' if _r1['ok'] else '❌'} {_uf1.name}" +
                        (f" → `{Path(_r1['txt_path']).name}`" if _r1["ok"] else f": {_r1['error']}")
                    )
                else:
                    _ph1 = st.empty()
                    _process_file_for_pipeline(
                        _uf1, _ws1, _nfc(_ws1), True, _tr_eng1,
                        False, False, _ph1, do_wiki=True,
                    )
                _prog1.progress(_i1 / len(_to_run1))
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


# ── 탭2: 장별 분할 ────────────────────────────────────────
with tab_split:
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

    # 폴더 선택 → 분할 대기 / 완료 목록 수집
    _fws2 = DEFAULT_WS
    _split_pend2: list[dict] = []
    _split_done2: list[dict] = []
    if _fws2 and DONE_DIR.exists():
        _t2 = DONE_DIR / _fws2 / TXT_SUB
        if _t2.exists():
            for _txt2 in sorted(_t2.glob("*.txt")):
                _stem2 = _nfc(_txt2.stem)
                _ch2 = chapters_dir(_fws2, _stem2)
                _ch_txts2 = [f for f in (_ch2.glob("??.*.txt") if _ch2.exists() else [])
                             if not f.stem.endswith(("_ko", "_wiki"))]
                _meta2 = f"{_txt2.stat().st_size//1024}KB"
                if _ch_txts2:
                    _split_done2.append({"ws": _fws2, "stem": _stem2,
                                          "n": len(_ch_txts2), "ch_dir": _ch2})
                else:
                    _split_pend2.append({"key": f"{_fws2}_{_stem2}", "label": _stem2,
                                          "meta": _meta2, "obj": {"ws": _fws2, "stem": _stem2}})

    st.markdown(f"#### 분할 대기 ({len(_split_pend2)}권)")
    if _split_pend2:
        _sel2 = _checklist(_split_pend2, "split2", height=280)
        _b2c1, _b2c2 = st.columns(2)
        _rs2 = _b2c1.button(f"▶ 선택 분할 ({len(_sel2)}권)", key="split2_run_sel",
                              use_container_width=True, type="primary", disabled=len(_sel2)==0)
        _ra2 = _b2c2.button(f"▶ 전체 분할 ({len(_split_pend2)}권)", key="split2_run_all",
                              use_container_width=True)
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
                _sp2.progress(_si2 / len(_to2))
            st.rerun()
    else:
        st.info("분할 대기 없음 — 1·OCR/TXT 탭에서 TXT를 먼저 생성하세요")

    st.divider()
    st.markdown(f"#### 분할 완료 ({len(_split_done2)}권)")
    if _split_done2:
        with st.container(height=240, border=True):
            for _sd2 in _split_done2:
                _sdc1, _sdc2, _sdc3 = st.columns([5, 1.5, 1])
                _sdc1.markdown(
                    f"**{_sd2['stem']}** &nbsp;<small style='color:#9ca3af'>"
                    f"[{_sd2['ws']}] · {_sd2['n']}챕터</small>",
                    unsafe_allow_html=True,
                )
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


# ── 탭3: 번역 ─────────────────────────────────────────────
with tab_tr:
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

        # TXT 직접 업로드 후 즉시 번역
        _up3 = st.file_uploader("TXT 직접 업로드 (즉시 번역)",
                                  type=["txt"], accept_multiple_files=True, key="tr3_uploader")
        if _up3:
            for _u3 in _up3:
                _tmp3 = Path(tempfile.gettempdir()) / _u3.name
                _tmp3.write_bytes(_u3.read())
                with st.status(f"번역 중: {_u3.name}", expanded=True):
                    _ok3u, _msg3u = translate_one_chapter(_tmp3, _tr_eng3)
                (st.success if _ok3u else st.error)(f"{'✅' if _ok3u else '❌'} {_u3.name}: {_msg3u}")
            st.rerun()

        # 폴더 선택 → 번역 대기 / 완료 수집
        _fws3 = DEFAULT_WS
        _tr_pend3: list[dict] = []
        _tr_done3 = 0
        if _fws3 and DONE_DIR.exists():
            _ch_root3 = DONE_DIR / _fws3 / "chapters"
            if _ch_root3.exists():
                for _book3 in sorted(_ch_root3.iterdir()):
                    if not _book3.is_dir():
                        continue
                    for _cf3 in sorted(_book3.glob("??.*.txt")):
                        if _cf3.stem.endswith(("_ko", "_wiki")):
                            continue
                        _ko3 = _cf3.with_name(_cf3.stem + "_ko.txt")
                        if _ko3.exists():
                            _tr_done3 += 1
                        else:
                            _tr_pend3.append({
                                "key": str(_cf3.relative_to(DONE_DIR)),
                                "label": f"{_book3.name} / {_cf3.name}",
                                "meta": f"{_cf3.stat().st_size//1024}KB",
                                "obj": _cf3,
                            })

        st.divider()
        st.markdown(f"#### 번역 대기 ({len(_tr_pend3)}개) / 완료 {_tr_done3}개")
        if _tr_pend3:
            _sel3 = _checklist(_tr_pend3, "tr3", height=280)
            _b3c1, _b3c2 = st.columns(2)
            _rs3 = _b3c1.button(f"▶ 선택 번역 ({len(_sel3)}개)", key="tr3_run_sel",
                                  use_container_width=True, type="primary", disabled=len(_sel3)==0)
            _ra3 = _b3c2.button(f"▶ 전체 번역 ({len(_tr_pend3)}개)", key="tr3_run_all",
                                  use_container_width=True)
            _to3 = _tr_pend3 and ([it["obj"] for it in _tr_pend3] if _ra3 else (_sel3 if _rs3 else []))
            if _to3:
                _tp3 = st.progress(0.0)
                for _ti3, _tf3 in enumerate(_to3, 1):
                    st.caption(f"번역 [{_ti3}/{len(_to3)}]: {_tf3.name}")
                    _ok3, _msg3 = translate_one_chapter(_tf3, _tr_eng3)
                    (st.success if _ok3 else st.warning)(
                        f"{'✅' if _ok3 else '⚠️'} {_tf3.name}: {_msg3}")
                    _tp3.progress(_ti3 / len(_to3))
                st.success(f"번역 처리 완료: {len(_to3)}개"); st.rerun()
        else:
            st.info("번역 대기 없음 — 2·장별분할 탭에서 챕터를 먼저 분리하세요")

    st.info("💡 다음 단계: **📝 4·요약MD** 탭으로 이동하세요")


# ── 탭4: 요약MD ───────────────────────────────────────────
with tab_summ:
    st.subheader("📝 요약MD 생성")
    st.caption("챕터 TXT(번역본 우선)로 Obsidian 노트용 요약 JSON을 생성합니다.")

    _wp4, _wm4 = llm.wiki_provider_model()
    _prov_ok4 = any(llm.has_key(p) for p in llm.PROVIDERS)
    if not _prov_ok4:
        st.warning("요약 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요.")
    else:
        st.caption(f"요약 모델: `{_wp4} · {_wm4}` — ⚙️ 설정 탭에서 변경")

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

        # 폴더 선택 → 요약 대기 / 완료 수집
        _fws4 = DEFAULT_WS
        _sum_pend4: list[dict] = []
        _sum_done4 = 0
        if _fws4 and DONE_DIR.exists():
            _ch_root4 = DONE_DIR / _fws4 / "chapters"
            if _ch_root4.exists():
                for _book4 in sorted(_ch_root4.iterdir()):
                    if not _book4.is_dir():
                        continue
                    _bstem4 = _nfc(_book4.name)
                    for _cf4 in sorted(_book4.glob("??.*.txt")):
                        if _cf4.stem.endswith(("_ko", "_wiki")):
                            continue
                        _json4 = _cf4.with_name(_cf4.stem + "_wiki.json")
                        if _json4.exists():
                            _sum_done4 += 1
                        else:
                            _ko4 = _cf4.with_name(_cf4.stem + "_ko.txt")
                            _tag4 = "🌐ko" if _ko4.exists() else "📄원문"
                            _sum_pend4.append({
                                "key": str(_cf4.relative_to(DONE_DIR)),
                                "label": f"{_book4.name} / {_cf4.name}",
                                "meta": f"{_tag4} · {_cf4.stat().st_size//1024}KB",
                                "obj": (_cf4, _bstem4),
                            })

        st.divider()
        st.markdown(f"#### 요약 대기 ({len(_sum_pend4)}개) / 완료 {_sum_done4}개")
        if _sum_pend4:
            _sel4 = _checklist(_sum_pend4, "summ4", height=280)
            _b4c1, _b4c2 = st.columns(2)
            _rs4 = _b4c1.button(f"▶ 선택 요약 ({len(_sel4)}개)", key="summ4_run_sel",
                                  use_container_width=True, type="primary", disabled=len(_sel4)==0)
            _ra4 = _b4c2.button(f"▶ 전체 요약 ({len(_sum_pend4)}개)", key="summ4_run_all",
                                  use_container_width=True)
            _to4: list = ([it["obj"] for it in _sum_pend4] if _ra4 else (_sel4 if _rs4 else []))
            if _to4:
                _sp4 = st.progress(0.0)
                for _si4, (_sf4, _bst4) in enumerate(_to4, 1):
                    with st.status(f"요약 [{_si4}/{len(_to4)}]: {_sf4.name}", expanded=False):
                        _ok4, _msg4 = summarize_one_chapter(_sf4, _bst4)
                    (st.success if _ok4 else st.warning)(
                        f"{'✅' if _ok4 else '⚠️'} {_sf4.name}: {_msg4[:80]}")
                    _sp4.progress(_si4 / len(_to4))
                st.success(f"요약 처리 완료: {len(_to4)}개"); st.rerun()
        else:
            st.info("요약 대기 없음 — 2·장별분할 탭에서 챕터를 먼저 분리하세요")

    st.info("💡 다음 단계: **📖 5·Wiki반영** 탭으로 이동하세요")


# ── 탭5: Wiki반영 ─────────────────────────────────────────
with tab_wiki5:
    st.subheader("📖 Obsidian Wiki 반영")
    st.caption("챕터 요약(_wiki.json)들을 합쳐 Obsidian 노트로 생성합니다.")

    _wiki_stems5 = {_nfc(p.stem) for p in WIKI_DIR.rglob("*.md")} if WIKI_DIR.exists() else set()

    # 폴더 선택
    _fws5 = DEFAULT_WS

    # 챕터 요약 기반 대기 목록
    _wiki_pend5: list[dict] = []
    _wiki_done5_list: list[dict] = []
    if _fws5 and DONE_DIR.exists():
        _ch_root5 = DONE_DIR / _fws5 / "chapters"
        if _ch_root5.exists():
            for _book5 in sorted(_ch_root5.iterdir()):
                if not _book5.is_dir():
                    continue
                _stem5 = _nfc(_book5.name)
                _jsons5 = list(_book5.glob("*_wiki.json"))
                if not _jsons5:
                    continue
                _total5 = len([f for f in _book5.glob("??.*.txt")
                               if not f.stem.endswith(("_ko", "_wiki"))])
                _ratio5 = f"{len(_jsons5)}/{_total5}챕터"
                if _stem5 in _wiki_stems5:
                    _wiki_done5_list.append({"stem": _stem5, "ws": _fws5,
                                              "n": len(_jsons5), "total": _total5})
                else:
                    _wiki_pend5.append({
                        "key": f"{_fws5}_{_stem5}",
                        "label": _stem5,
                        "meta": f"{_ratio5} 요약됨",
                        "obj": {"ws": _fws5, "stem": _stem5},
                    })

    # 단일 TXT 기반 (챕터 분할 없는 책)
    _single_pend5: list[dict] = []
    if _fws5 and DONE_DIR.exists():
        _t5s = DONE_DIR / _fws5 / TXT_SUB
        if _t5s.exists():
            for _txt5s in sorted(_t5s.glob("*.txt")):
                _stem5s = _nfc(_txt5s.stem)
                _ch5s = chapters_dir(_fws5, _stem5s)
                if _ch5s.exists() and any(f for f in _ch5s.glob("??.*.txt")
                                           if not f.stem.endswith(("_ko","_wiki"))):
                    continue
                if _stem5s in _wiki_stems5:
                    continue
                _single_pend5.append({
                    "key": f"s_{_fws5}_{_stem5s}",
                    "label": _stem5s,
                    "meta": f"단일TXT · {_txt5s.stat().st_size//1024}KB",
                    "obj": {"ws": _fws5, "stem": _stem5s, "txt": _txt5s},
                })

    # 챕터 요약 → Wiki
    st.markdown(f"#### 챕터 요약 → Wiki ({len(_wiki_pend5)}권 대기)")
    if _wiki_pend5:
        _sel5 = _checklist(_wiki_pend5, "wiki5", height=240)
        _b5c1, _b5c2 = st.columns(2)
        _rs5 = _b5c1.button(f"▶ 선택 Wiki생성 ({len(_sel5)}권)", key="wiki5_run_sel",
                              use_container_width=True, type="primary", disabled=len(_sel5)==0)
        _ra5 = _b5c2.button(f"▶ 전체 Wiki생성 ({len(_wiki_pend5)}권)", key="wiki5_run_all",
                              use_container_width=True)
        _to5 = ([it["obj"] for it in _wiki_pend5] if _ra5 else (_sel5 if _rs5 else []))
        if _to5:
            _wp5 = st.progress(0.0)
            for _wi5, _wo5 in enumerate(_to5, 1):
                with st.status(f"Wiki [{_wi5}/{len(_to5)}]: {_wo5['stem']}", expanded=False):
                    _ok5, _msg5 = build_wiki_from_chapter_summaries(_wo5["ws"], _wo5["stem"])
                (st.success if _ok5 else st.error)(
                    f"{'✅' if _ok5 else '❌'} {_wo5['stem']}: "
                    f"{Path(_msg5).name if _ok5 else _msg5}")
                _wp5.progress(_wi5 / len(_to5))
            st.balloons() if all(it["obj"] in [_wo5] for _wo5 in _to5) else None
            st.rerun()
    else:
        st.info("챕터 요약 기반 Wiki 대기 없음 — 4·요약MD 탭에서 요약을 먼저 실행하세요")

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
        if _wv_col1.button("📓 Obsidian 금고 열기", key="w5_vault", use_container_width=True):
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


# ── 탭: 설정 (API 키) ─────────────────────────────────────
with tab_settings:
    st.subheader("⚙️ API 키 설정")
    st.caption(
        "키는 이 컴퓨터의 `~/.config/mybookshelf/keys.json` 에만 저장되며, "
        "저장소나 외부로 전송되지 않습니다. (Gemini 키는 위키 생성기와 자동 공유됩니다.)"
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
        with st.expander(f"{_info['label']}  —  {('✅ ' + _cur) if _cur else '미설정'}",
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
                    st.info("삭제됨")
                    st.rerun()
            st.caption(f"모델: {', '.join(_info['models'])}")
    st.divider()
    st.markdown("**🖥 CLI 구독 도구** — API 키 없이 구독으로 사용")
    _cc1, _cc2 = st.columns(2)
    with _cc1:
        st.markdown("**Claude CLI**")
        if llm.claude_cli_available():
            st.success(f"✅ 감지됨\n`{llm.claude_cli_path()}`")
        else:
            st.info("미설치. `npm install -g @anthropic-ai/claude-code`")
    with _cc2:
        st.markdown("**Codex CLI**")
        if llm.codex_cli_available():
            _cstatus = "로그인됨" if True else ""
            st.success(f"✅ 감지됨\n`{llm.codex_cli_path()}`")
            st.caption("ChatGPT 계정 또는 API 키로 로그인 필요: `codex login --device-auth`")
        else:
            st.info("미설치. `npm install -g @openai/codex`")

    st.divider()
    st.subheader("📓 위키 저장 폴더 (옵시디언 금고)")
    st.caption(
        f"현재: `{WIKI_DIR}` — 생성된 위키 노트가 여기 저장되고, "
        "Wiki 목록 탭의 [옵시디언에서 위키 금고 열기]도 이 폴더를 엽니다."
    )
    _default_wiki = str(cfg.BASE_DIR / "wiki")
    _wiki_cands: list[str] = []
    for _c in [_default_wiki] + list_obsidian_vaults():
        if _c and _c not in _wiki_cands:
            _wiki_cands.append(_c)
    _cur_wiki = str(WIKI_DIR)
    _wd_sel = st.selectbox(
        "폴더 선택 — 기본값 + 옵시디언에 등록된 금고들",
        _wiki_cands,
        index=_wiki_cands.index(_cur_wiki) if _cur_wiki in _wiki_cands else 0,
        key="wiki_dir_sel",
    )
    _wd_custom = st.text_input("또는 폴더 경로 직접 입력 (비우면 위 선택 사용)", value="", key="wiki_dir_custom")
    _wd_target = (_wd_custom.strip() or _wd_sel).strip()
    if st.button("💾 위키 폴더 저장", use_container_width=True, key="wiki_dir_save"):
        if _wd_target == _cur_wiki:
            st.info("이미 이 폴더를 쓰고 있습니다.")
        else:
            set_wiki_dir(_wd_target)
            st.success(f"저장됨: `{_wd_target}`")
            st.warning("⚠️ 앱을 재시작해야 적용됩니다 — stop-app.bat 실행 후 start-app.vbs.")
    st.caption("ℹ️ 기존에 만든 노트는 자동으로 옮겨지지 않습니다. 옮기려면 폴더에서 직접 이동하세요.")
