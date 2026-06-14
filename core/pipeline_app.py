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


# ── 다국어 번역 (2026-06-13 v0.4.0) ──────────────────────────
# 원문 언어는 LLM이 자동 감지. 목표 언어만 설정탭에서 고른다(기본 한국어).
# script_re가 있는 언어는 "문서가 이미 목표 언어인지"를 문자 비율로 판정해 번역을 건너뛴다.
# 라틴 문자 언어(en·de·fr 등)는 문자만으로 구별 불가 → 항상 번역 시도(원문=목표어면 낭비지만 무해).
# fmt: (한국어명, English name, Unicode script regex | None)
LANGS: dict[str, tuple[str, str, str | None]] = {
    # ── 동아시아 ──────────────────────────────────
    "ko":    ("한국어",                   "Korean",              r"[가-힣]"),
    "zh":    ("중국어 (간체)",             "Simplified Chinese",  r"[一-鿿]"),
    "zh-tw": ("중국어 (번체)",             "Traditional Chinese", r"[一-鿿]"),
    "ja":    ("일본어",                   "Japanese",            r"[぀-ヿ]"),
    "mn":    ("몽골어",                   "Mongolian",           r"[᠀-᢯]"),
    # ── 동남아시아 ────────────────────────────────
    "th":    ("태국어",                   "Thai",                r"[฀-๿]"),
    "km":    ("크메르어 (캄보디아)",        "Khmer",               r"[ក-៿]"),
    "vi":    ("베트남어",                 "Vietnamese",          None),
    "id":    ("인도네시아어",              "Indonesian",          None),
    "ms":    ("말레이어",                 "Malay",               None),
    "tl":    ("타갈로그어 (필리핀)",       "Filipino/Tagalog",    None),
    "my":    ("미얀마어",                 "Burmese/Myanmar",     r"[က-႟]"),
    "lo":    ("라오어",                   "Lao",                 r"[຀-໿]"),
    # ── 남아시아 ──────────────────────────────────
    "hi":    ("힌디어",                   "Hindi",               r"[ऀ-ॿ]"),
    "ne":    ("네팔어",                   "Nepali",              r"[ऀ-ॿ]"),
    "bn":    ("벵골어 (방글라데시)",       "Bengali",             r"[ঀ-৿]"),
    "si":    ("싱할라어 (스리랑카)",       "Sinhala",             r"[඀-෿]"),
    "ur":    ("우르두어 (파키스탄)",       "Urdu",                r"[؀-ۿ]"),
    # ── 중앙아시아 · 러시아 ───────────────────────
    "ru":    ("러시아어",                 "Russian",             r"[Ѐ-ӿ]"),
    "kk":    ("카자흐어",                 "Kazakh",              None),
    "uz":    ("우즈베크어",               "Uzbek",               None),
    "ky":    ("키르기스어",               "Kyrgyz",              None),
    "tg":    ("타지크어",                 "Tajik",               None),
    # ── 중동 ──────────────────────────────────────
    "ar":    ("아랍어",                   "Arabic",              r"[؀-ۿ]"),
    "fa":    ("페르시아어 (이란)",         "Persian/Farsi",       r"[؀-ۿ]"),
    "tr":    ("터키어",                   "Turkish",             None),
    "he":    ("히브리어",                 "Hebrew",              r"[֐-׿]"),
    "ku":    ("쿠르드어",                 "Kurdish",             None),
    # ── 아프리카 ──────────────────────────────────
    "am":    ("암하라어 (에티오피아)",     "Amharic",             r"[ሀ-፿]"),
    "ti":    ("티그리냐어 (에리트레아)",   "Tigrinya",            r"[ሀ-፿]"),
    "sw":    ("스와힐리어",               "Swahili",             None),
    "ha":    ("하우사어 (나이지리아)",     "Hausa",               None),
    "yo":    ("요루바어 (나이지리아)",     "Yoruba",              None),
    "ig":    ("이그보어 (나이지리아)",     "Igbo",                None),
    "so":    ("소말리아어",               "Somali",              None),
    "mg":    ("말라가시어 (마다가스카르)", "Malagasy",            None),
    # ── 서방 · 유럽 ───────────────────────────────
    "en":    ("영어",                     "English",             None),
    "de":    ("독일어",                   "German",              None),
    "fr":    ("프랑스어",                 "French",              None),
    "es":    ("스페인어",                 "Spanish",             None),
    "pt":    ("포르투갈어",               "Portuguese",          None),
    "nl":    ("네덜란드어",               "Dutch",               None),
}

# 지역별 언어 그룹 (설정 UI 2단계 선택용)
LANG_REGIONS: dict[str, list[str]] = {
    "🌏 동아시아":          ["ko", "zh", "zh-tw", "ja", "mn"],
    "🌴 동남아시아":        ["th", "km", "vi", "id", "ms", "tl", "my", "lo"],
    "🕌 남아시아":          ["hi", "ne", "bn", "si", "ur"],
    "🏔 중앙아시아·러시아":  ["ru", "kk", "uz", "ky", "tg"],
    "☪️ 중동":              ["ar", "fa", "tr", "he", "ku"],
    "🌍 아프리카":          ["am", "ti", "sw", "ha", "yo", "ig", "so", "mg"],
    "🌎 서방·유럽":         ["en", "de", "fr", "es", "pt", "nl"],
}


def target_lang() -> str:
    code = (llm.get_pref("target_lang") or "ko").strip()
    return code if code in LANGS else "ko"


def needs_translation(txt_path: Path, threshold: float = 0.3) -> bool:
    """문서가 목표 언어 문자로 threshold 이상이면 이미 목표 언어로 보고 번역 스킵."""
    script = LANGS[target_lang()][2]
    if not script:
        return True   # 라틴계 목표 언어: 문자 비율로 판정 불가 → 번역 시도
    sample = txt_path.read_text(encoding="utf-8", errors="ignore")[:3000]
    hits = len(_re.findall(script, sample))
    return (hits / max(len(sample), 1)) < threshold


def is_english(txt_path: Path, threshold: float = 0.3) -> bool:
    """(구버전 호환) 목표 언어 기준 번역 필요 여부."""
    return needs_translation(txt_path, threshold)


def build_translate_system(code: str) -> str:
    """목표 언어별 번역 시스템 프롬프트. 한국어일 때만 평서체 규칙 추가."""
    _, en_name, _ = LANGS[code]
    p = (
        f"You are a professional theological/academic translator. "
        f"Detect the source language automatically and translate the user's text into {en_name}. "
        f"Proper nouns (personal names, place names): on FIRST mention write the {en_name} "
        f"rendering followed by the original in parentheses; "
        f"if a name is listed below as already introduced, write the {en_name} form ONLY. "
        "Preserve technical terms and scripture references as-is. "
    )
    if code == "ko":
        p += (
            "Use ONLY plain declarative academic Korean (평서체/하다체): "
            "endings such as -다, -이다, -한다, -였다, -이었다. "
            "DO NOT use any polite/honorific forms — never use -습니다, -입니다, "
            "-해요, -이에요, -지요, -군요, -네요, or any other -요/-니다 endings. "
        )
    else:
        p += f"Use a formal academic register appropriate for scholarly prose in {en_name}. "
    p += (
        "The text may be an incomplete fragment cut mid-sentence (PDF page breaks): "
        "translate it as-is anyway — NEVER comment on it, NEVER ask for more context, "
        "NEVER say the text is incomplete. "
        f"Output ONLY the {en_name} translation, nothing else."
    )
    return p

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
    sys_prompt = build_translate_system(target_lang())
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
            _tgt_name = LANGS[target_lang()][0]
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
    """단락 문자의 threshold 이상이 이미 목표 언어 스크립트면 True (API 호출 불필요).
    라틴계 목표 언어(영·불·독 등)는 소스 언어와 구분이 어려우므로 항상 False."""
    script = LANGS[target_lang()][2]
    if not script:
        return False
    p = paragraph.strip()
    if not p:
        return False
    hits = len(_re.findall(script, p))
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
st.caption("PDF 업로드 → OCR/번역 → 텍스트 내용 요약 Obsidian Wiki 자동 생성")

# 상태 배너
col_s1, col_s2, col_s3 = st.columns(3)
_avail_providers = [info["label"] for prov, info in llm.PROVIDERS.items() if llm.has_key(prov)]
if llm.claude_cli_available():
    _avail_providers.append("Claude CLI")
_gemini_key_ok = llm.has_key("gemini")
wg_ok = wiki_generator_running()

col_s1.metric("API 키", f"{len(_avail_providers)}개" if _avail_providers else "❌ 없음")
col_s2.metric("위키 생성기", "🔄 생성 중" if wg_ok else "대기")
col_s3.metric("Wiki 완성", sum(1 for _ in WIKI_DIR.rglob("*.md")))

if not _avail_providers:
    st.error("⚠️ 사용 가능한 API가 없습니다 — ⚙️ 설정 탭에서 키를 입력하세요.")
elif not _gemini_key_ok:
    st.warning("ℹ️ 위키 생성은 Gemini 키가 필요합니다 — ⚙️ 설정 탭에서 Google Gemini 키를 입력하세요.")

# Wiki raw 고아 감지 게이트 (2026-05-16) — raw/processed 이동 버그 흔적 자동 표시
_wiki_orphans = check_wiki_orphans()
if _wiki_orphans["wiki_done_raw_remaining"] > 0:
    with st.expander(
        f"⚠️ Wiki 생성 후 raw 사이드카 미이동 {_wiki_orphans['wiki_done_raw_remaining']}건 "
        f"(raw/processed 이동 버그 흔적)",
        expanded=False,
    ):
        st.write(
            "Wiki 본문(.md)은 정상 생성됐으나 RAW_DIR의 .txt가 PROCESSED_DIR로 이동되지 않았습니다. "
            "Wiki 자체는 사용 가능합니다. 정리 명령:"
        )
        for f in _wiki_orphans["orphan_files"]:
            st.code(f"mv \"{f}\" \"{PROCESSED_DIR}/\"", language="bash")

# ── 사이드바 — 비움 (2026-05-18, 모든 컨트롤을 탭으로 이전) ──────

# ── 데이터 사전 계산 (탭 라벨에 카운트 표시용) ────────────────
_failed_files = []
if FAILED_DIR.exists():
    for p in FAILED_DIR.rglob("*"):
        if p.is_file():
            _failed_files.append(p)
    _failed_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

_done_count = 0
if DONE_DIR.exists():
    for ws in DONE_DIR.iterdir():
        if not ws.is_dir(): continue
        _done_count += sum(1 for p in ws.glob("*.pdf") if p.is_file())
_wiki_count = sum(1 for _ in WIKI_DIR.rglob("*.md"))

# ── 탭 6개 — 라벨에 동적 카운트 ────────────────────────────
_failed_label = f"⚠️ 실패 파일 ({len(_failed_files)})" if _failed_files else "⚠️ 실패 파일"

tab_upload, tab_history, tab_wiki, tab_failed, tab_status, tab_settings = st.tabs([
    "📤 파일 업로드",
    "📁 처리 기록",
    "📖 Wiki 목록",
    _failed_label,
    "📊 현황",
    "⚙️ 설정",
])

# ── 탭 0: 현황 ───────────────────────────────────────────
with tab_status:
    c1, c2, c3 = st.columns(3)
    c1.metric("완료 (PDF)", _done_count)
    c2.metric("실패", len(_failed_files))
    c3.metric("Wiki", _wiki_count)

    # ─── 🔄 진행 중 파일 패널 (창 닫아도 복원, 2026-05-19) ─────────────
    # bilingual.txt 갱신 시각·EN/KO 카운트로 추정. 5분 내 갱신 = 진행 중
    import time as _t
    _now = _t.time()
    in_progress = []
    recent_completed = []
    if DONE_DIR.exists():
        for ws_dir in DONE_DIR.iterdir():
            if not ws_dir.is_dir(): continue
            tr = ws_dir / TRANS_SUB
            if not tr.exists(): continue
            for bil in tr.glob("*_bilingual.txt"):
                age_s = _now - bil.stat().st_mtime
                try:
                    text = bil.read_text(encoding="utf-8", errors="ignore")
                    if "\n\n[KO]\n" in text:         # 구형
                        en = text.count("[EN]\n")
                        ko = text.count("\n\n[KO]\n")
                    else:                             # 신형 태그 없는 형식
                        all_blocks = [b.strip() for b in text.split("\n\n---\n\n") if b.strip()]
                        en = len(all_blocks)
                        ko = sum(1 for b in all_blocks if "\n\n" in b and not b.startswith("["))
                except Exception:
                    continue
                if age_s < 300:  # 5분 내 갱신 = 진행 중
                    in_progress.append({
                        "ws": ws_dir.name, "stem": bil.stem.removesuffix("_bilingual"),
                        "en": en, "ko": ko, "age_s": age_s,
                        "pct": (ko / en * 100) if en else 0,
                    })
                elif age_s < 3600 and en > ko:  # 1시간 내 갱신 + 결손 = 최근 중단
                    recent_completed.append({
                        "ws": ws_dir.name, "stem": bil.stem.removesuffix("_bilingual"),
                        "en": en, "ko": ko, "age_s": age_s,
                        "pct": (ko / en * 100) if en else 0,
                    })

    if in_progress:
        st.divider()
        st.subheader(f"🔄 진행 중 ({len(in_progress)}건)")
        st.caption("최근 5분 내 갱신된 번역 작업. 페이지 새로고침으로 진행률 갱신.")
        if st.button("🔄 갱신", key="status_refresh_inprog"):
            st.rerun()
        for p in sorted(in_progress, key=lambda x: x["age_s"]):
            age_str = f"{int(p['age_s'])}초 전" if p['age_s'] < 60 else f"{int(p['age_s']/60)}분 전"
            _paused = is_paused(p['stem'])
            cols = st.columns([3.5, 1, 1, 1, 1])
            label_suffix = " ⏸️" if _paused else ""
            cols[0].markdown(
                f"**{p['stem'][:50]}**{label_suffix}\n\n<small>[{p['ws']}] · 갱신 {age_str}</small>",
                unsafe_allow_html=True,
            )
            cols[1].metric("EN", p['en'])
            cols[2].metric("KO", p['ko'])
            cols[3].metric("%", f"{p['pct']:.0f}%")
            if _paused:
                if cols[4].button("▶️ 재개", key=f"resume_{p['ws']}_{p['stem']}", use_container_width=True):
                    set_paused(p['stem'], False)
                    st.rerun()
            else:
                if cols[4].button("⏸️ 정지", key=f"pause_{p['ws']}_{p['stem']}", use_container_width=True):
                    set_paused(p['stem'], True)
                    st.rerun()
            st.progress(p['pct'] / 100 if p['en'] else 0)

    if recent_completed:
        with st.expander(f"⏸️ 최근 1시간 내 중단된 작업 ({len(recent_completed)}건)", expanded=False):
            st.caption("결손이 있는 상태로 멈춤. Resume 가능 (📤 업로드 탭에서 동일 PDF 재업로드).")
            for p in sorted(recent_completed, key=lambda x: x["age_s"]):
                age_str = f"{int(p['age_s']/60)}분 전"
                st.caption(
                    f"• **{p['stem']}** [{p['ws']}] — "
                    f"EN={p['en']}/KO={p['ko']} · 결손 {p['en']-p['ko']} · {age_str}"
                )

    # 위키 수동 트리거 (노트 없는 TXT를 Gemini로 일괄 생성)
    raw_pending = check_wiki_orphans().get("raw_pending", 0)
    if raw_pending > 0:
        st.divider()
        st.info(f"위키 생성 대기: {raw_pending}개")
        if not wg_ok:
            if st.button("▶️ 위키 생성 시작", use_container_width=True, key="status_wiki_trigger"):
                trigger_wiki_generation()
                st.rerun()
        else:
            st.caption("🔄 위키 생성 중...")


# ── 탭 1: 업로드 ─────────────────────────────────────────
with tab_upload:
    force_reembed = False   # 임베드 제거(2026-06-09) — 호환용 상수
    defer_embed   = False
    ws_name = next(iter(WORKSPACES))
    ws_slug = ""

    # ── ① 번역 ───────────────────────────────────────────
    st.markdown("#### ① 번역")
    do_translate = st.toggle(
        f"🌐 번역  →  {LANGS[target_lang()][0]}",
        value=bool(llm.get_pref("do_translate", False)),
        help="원문을 목표 언어로 번역한 파일을 함께 만듭니다. "
             "원문이 이미 목표 언어이면 자동으로 건너뜁니다. "
             "위키 노트는 번역 여부와 무관하게 항상 한국어로 생성됩니다.",
    )
    llm.set_pref("do_translate", bool(do_translate))
    if do_translate:
        _tc1, _tc2, _tc3 = st.columns([1.2, 1.5, 2.0])
        _cur_tgt2 = target_lang()
        _cur_region2 = next(
            (r for r, codes in LANG_REGIONS.items() if _cur_tgt2 in codes),
            list(LANG_REGIONS.keys())[0]
        )
        _sel_region2 = _tc1.selectbox(
            "지역", list(LANG_REGIONS.keys()),
            index=list(LANG_REGIONS.keys()).index(_cur_region2),
            key="upload_region_sel",
        )
        _region_codes2 = LANG_REGIONS[_sel_region2]
        _def_idx2 = _region_codes2.index(_cur_tgt2) if _cur_tgt2 in _region_codes2 else 0
        _tgt_sel2 = _tc2.selectbox(
            "목표 언어", _region_codes2, index=_def_idx2,
            format_func=lambda c: f"→ {LANGS[c][0]}",
            key="upload_lang_sel",
            help="문서가 이미 목표 언어면 자동 스킵됩니다. "
                 "라틴 문자 언어(영어·독일어 등)는 스킵 판정 불가 — 같은 언어 PDF는 번역 토글을 끄세요.",
        )
        if _tgt_sel2 != _cur_tgt2:
            llm.set_pref("target_lang", _tgt_sel2)
        _opts = [(eid, lbl) for eid, lbl, av, _h in translate_engine_options() if av]
        if not _opts:
            _tc3.warning("⚠️ 번역 엔진 없음 — ⚙️ 설정에서 API 키 입력")
            translate_engine = None
        else:
            _lbl2id = {lbl: eid for eid, lbl in _opts}
            _labels = list(_lbl2id.keys())
            _saved_eng = llm.get_pref("translate_engine")
            _idx = next((i for i, l in enumerate(_labels) if _lbl2id[l] == _saved_eng), 0)
            _sel = _tc3.selectbox(
                "번역 엔진", _labels, index=_idx,
                key="upload_engine_sel",
                help="키가 등록된 공급자만 표시됩니다. ⚙️ 설정 탭에서 키를 관리하세요.",
            )
            translate_engine = _lbl2id[_sel]
            llm.set_pref("translate_engine", translate_engine)
            if translate_engine.startswith("claude_cli:"):
                st.caption("ℹ️ Claude 구독(CLI)로 호출 — 주간 한도가 차감됩니다.")
    else:
        translate_engine = None

    st.divider()
    # ── ② 파일 업로드 ─────────────────────────────────────
    st.markdown("#### ② 파일 업로드")
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0
    if "pipeline_results" not in st.session_state:
        st.session_state.pipeline_results = []   # 새 세션은 항상 빈 결과 (이전 기록은 📁 처리기록 탭)

    uploaded_files = st.file_uploader(
        "PDF / DOCX / TXT / MD를 드래그하거나 클릭하여 선택",
        type=["pdf", "docx", "txt", "md"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_key}",
    )

    # ─── 재시도 대기 자동 감지 (UPLOAD_TMP의 모든 파일, 2026-05-18 갱신) ─
    # UPLOAD_TMP를 *재시도 대기 큐*로 본다. session_state 의존 없이 물리적 상태 기반.
    _allowed_ext = {".pdf", ".docx", ".doc", ".txt", ".md"}

    # (임베드 큐 기반 자동정리 제거 — 2026-06-09)

    retry_paths = sorted(
        [
            p for p in UPLOAD_TMP.glob("*")
            if p.is_file() and p.suffix.lower() in _allowed_ext
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # 같은 이름의 PDF가 대기 중이면 그 부산물(.md/.txt 사이드카)은 숨김 —
    # 실패 잔재이며 PDF 재처리가 같은 자리에 다시 만든다. (2026-06-11)
    _pdf_stems = {p.stem for p in retry_paths if p.suffix.lower() == ".pdf"}
    retry_paths = [p for p in retry_paths
                   if not (p.suffix.lower() in {".md", ".txt"} and p.stem in _pdf_stems)]
    if retry_paths:
        import time as _t
        _now = _t.time()
        sel_retry = [
            p for p in retry_paths
            if st.session_state.get(f"upload_retry_sel_{p}", False)
        ]
        sel_n = len(sel_retry)
        with st.expander(
            f"↩️ 재시도 대기: **{len(retry_paths)}개**"
            + (f" · 선택 **{sel_n}건**" if sel_n else "") + " (자동 합산됨)",
            expanded=True,
        ):
            # 전체 선택 토글
            _select_all_key = "upload_retry_select_all"
            def _toggle_all():
                v = st.session_state.get(_select_all_key, False)
                for p in retry_paths:
                    st.session_state[f"upload_retry_sel_{p}"] = v
            st.checkbox(
                f"☑️ 전체 선택 ({len(retry_paths)}개)",
                key=_select_all_key,
                on_change=_toggle_all,
            )

            # 일괄 작업 버튼
            _btn_cols = st.columns([1, 1, 1, 1])
            if _btn_cols[0].button(
                f"▶️ 선택 {sel_n}건 재시도" if sel_n else "▶️ 선택 재시도",
                type="primary",
                disabled=(sel_n == 0), use_container_width=True,
                key="upload_retry_run_btn",
                help="선택된 파일만 즉시 처리 (워크스페이스·토글은 상단 설정 사용)",
            ):
                # 선택된 파일만 _PathAsUpload로 감싸 처리
                _to_run = list(sel_retry)
                results = []
                phs = [st.empty() for _ in _to_run]
                for _i, p in enumerate(_to_run):
                    r = _process_file_for_pipeline(
                        _PathAsUpload(p), ws_name, ws_slug, do_translate, translate_engine,
                        force_reembed, defer_embed, phs[_i], do_wiki=do_wiki,
                    )
                    results.append(r)
                    # 처리 끝난 파일은 UPLOAD_TMP에서 사라졌을 가능성 — 체크박스 상태도 정리
                    st.session_state.pop(f"upload_retry_sel_{p}", None)
                st.session_state.pipeline_results = (
                    results + st.session_state.get("pipeline_results", [])
                )
                save_pipeline_results(st.session_state.pipeline_results)
                st.success(f"🏁 {len(results)}건 처리 완료")
            if _btn_cols[1].button(
                f"🗑️ 선택 {sel_n}건 삭제" if sel_n else "🗑️ 선택 삭제",
                disabled=(sel_n == 0), use_container_width=True,
                key="upload_retry_del_btn",
            ):
                for p in sel_retry:
                    try: p.unlink()
                    except Exception: pass
                st.rerun()
            if _btn_cols[2].button(
                "🗑️ 전체 비우기", use_container_width=True,
                key="upload_retry_clear_btn",
            ):
                for p in retry_paths:
                    try: p.unlink()
                    except Exception: pass
                st.rerun()
            # 4번째 빈 컬럼 (정렬용)

            st.divider()

            # 각 행: 체크박스·파일명·이유·▶️·🗑️
            for p in retry_paths:
                try:
                    size_kb = p.stat().st_size // 1024
                    age_s = _now - p.stat().st_mtime
                except Exception:
                    size_kb, age_s = 0, 0
                # 이유 추정
                if age_s < 60:
                    reason = "🔄 방금 추가됨 (진행 중일 수)"
                elif age_s < 300:
                    reason = f"⏸️ {int(age_s/60)}분 전 추가 — 자동 합산 대기"
                elif age_s < 3600:
                    reason = f"⏳ {int(age_s/60)}분 전 — 처리 시작 안 됨"
                elif age_s < 86400:
                    reason = f"⏳ {int(age_s/3600)}시간 전 — 옛 대기"
                else:
                    reason = f"⚠️ {int(age_s/86400)}일 전 — 매우 옛 (정리 권장)"
                cols = st.columns([0.4, 4.5, 1.6, 0.6, 0.6])
                cols[0].checkbox(
                    "선택", key=f"upload_retry_sel_{p}",
                    label_visibility="collapsed",
                )
                cols[1].markdown(
                    f"**{p.name[:55]}**" + ("…" if len(p.name) > 55 else "") + "\n\n"
                    f"<small>{size_kb}KB · {reason}</small>",
                    unsafe_allow_html=True,
                )
                cols[2].caption("")  # 여백 (다른 정보 자리)
                if cols[3].button(
                    "▶️", key=f"upload_retry_run_single_{p}",
                    help="이 파일만 즉시 처리 (상단 워크스페이스·토글 적용)",
                ):
                    _ph = st.empty()
                    r = _process_file_for_pipeline(
                        _PathAsUpload(p), ws_name, ws_slug, do_translate, translate_engine,
                        force_reembed, defer_embed, _ph, do_wiki=do_wiki,
                    )
                    st.session_state.pipeline_results = (
                        [r] + st.session_state.get("pipeline_results", [])
                    )
                    save_pipeline_results(st.session_state.pipeline_results)
                    st.success(f"🏁 {p.name} 처리 완료")
                if cols[4].button(
                    "🗑️", key=f"upload_retry_del_single_{p}",
                    help="이 파일만 삭제",
                ):
                    try: p.unlink()
                    except Exception: pass
                    st.rerun()

    st.divider()
    # ── ③ 처리 옵션 ──────────────────────────────────────
    st.markdown("#### ③ 처리 옵션")
    _oc1, _oc2 = st.columns(2)
    _oc1.checkbox("📄 TXT 추출 (항상 실행)", value=True, disabled=True,
                  help="PDF·DOCX를 텍스트로 변환합니다 (기본, 해제 불가).")
    do_wiki = _oc2.toggle(
        "📓 위키 저장",
        value=bool(llm.get_pref("do_wiki", True)),
        help="처리 완료 후 Gemini가 노트를 생성합니다. 끄면 TXT/MD만 만들고 위키는 건너뜁니다.",
    )
    llm.set_pref("do_wiki", bool(do_wiki))
    if do_wiki:
        _vault_opts = [str(WIKI_DIR)]
        for _v in list_obsidian_vaults():
            if _v and str(Path(_v)) not in _vault_opts:
                _vault_opts.append(str(Path(_v)))
        if len(_vault_opts) > 1:
            if "wiki_target_dir" not in st.session_state:
                _saved_v = (llm.get_pref("wiki_target_dir") or "").strip()
                st.session_state.wiki_target_dir = _saved_v if _saved_v in _vault_opts else _vault_opts[0]
            st.selectbox(
                "📓 위키 저장 금고",
                _vault_opts, key="wiki_target_dir",
                help="첫 항목이 기본(⚙️ 설정의 위키 폴더), 나머지는 옵시디언에 등록된 금고들.",
            )
            llm.set_pref("wiki_target_dir", st.session_state.wiki_target_dir)
            if st.session_state.wiki_target_dir != _vault_opts[0]:
                st.caption(f"ℹ️ 이번 생성 노트는 `{st.session_state.wiki_target_dir}` 금고로 들어갑니다.")
    with st.expander("⚙️ 고급 설정"):
        _skip_proc = st.toggle(
            "⏭️ 이미 처리된 파일 건너뛰기",
            value=bool(llm.get_pref("skip_processed", True)),
            help="완료 폴더 산출물·위키 기록과 파일명을 대조해 이미 처리한 파일은 다시 돌리지 않습니다. "
                 "끄면 같은 파일도 강제로 재처리합니다(노트는 같은 이름으로 덮어씀).",
        )
        llm.set_pref("skip_processed", bool(_skip_proc))
        st.session_state["skip_processed_flag"] = bool(_skip_proc)

    st.divider()
    # 업로드 파일 + 재시도 대기 파일 합치기 (file-like wrapper은 모듈 레벨에 정의됨)

    all_files = list(uploaded_files or []) + [_PathAsUpload(p) for p in retry_paths]

    if all_files:
        st.info(f"**{len(all_files)}개** 파일 업로드 준비")


        if st.button("🚀 파이프라인 실행", type="primary", use_container_width=True):
            results = []
            # 워커별 placeholder 미리 생성 (병렬 격리용)
            _placeholders = [st.empty() for _ in all_files]

            # 단계 2: 병렬 처리 (max 4 동시, 2026-05-19)
            # add_script_run_ctx로 워커 스레드에 Streamlit context 전달 → 워커 안에서 st.write 등 호출 가능
            # 워커별 placeholder 격리로 UI 충돌 차단
            import threading
            from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

            _ctx = get_script_run_ctx()
            _results_lock = threading.Lock()
            _sem = threading.Semaphore(4)  # 동시 4개 제한 (Claude 한도 보호)

            def _worker(idx, uf):
                with _sem:
                    r = _process_file_for_pipeline(
                        uf, ws_name, ws_slug, do_translate, translate_engine,
                        force_reembed, defer_embed, _placeholders[idx], do_wiki=do_wiki,
                    )
                with _results_lock:
                    results.append(r)

            _threads = []
            for _idx, uf in enumerate(all_files):
                _t = threading.Thread(target=_worker, args=(_idx, uf))
                add_script_run_ctx(_t, _ctx)
                _t.start()
                _threads.append(_t)

            for _t in _threads:
                _t.join()

            st.session_state.pipeline_results = results
            save_pipeline_results(results)
            st.session_state.uploader_key += 1
            # 재시도 대기 파일은 처리 완료된 것만 정리 (UPLOAD_TMP에 없으면 자동 제거)
            st.session_state.retry_pending_files = [
                p for p in st.session_state.get("retry_pending_files", [])
                if Path(p).exists()
            ]
            # st.rerun() 제거 (2026-05-19) — 워커 placeholder UI를 그대로 두어
            # 사용자가 멈춤 사유·진행 상태를 *영구 확인* 가능. 사용자가 탭 전환·새로고침 시 자연 rerun.
            st.success(
                f"🏁 일괄 처리 종료 — 총 {len(results)}건. "
                f"위 각 워커 영역에서 결과 확인. 새 작업하려면 '🚀 파이프라인 실행' 다시."
            )

    if st.session_state.pipeline_results:
        st.divider()
        st.subheader("📂 처리 결과")

        _STAGE_ICONS = {"ok": "✅", "fail": "❌", "skip": "⏭️", "pending": "⏳", "queued": "⏸️"}
        _STAGE_LABELS = [
            ("ocr",         "① PDF OCR"),
            ("txt",         "② TXT 변환"),
            ("md",          "③ MD 변환"),
            ("bilingual",   "④ Bilingual"),
            ("wiki",        "⑤ Gemini Wiki"),
        ]

        # Wiki 산출물 stem 집합 — 매번 rglob 안 하도록 한 번만 빌드 (2026-05-18)
        _wiki_md_stem_set = {p.stem for p in WIKI_DIR.rglob("*.md")}

        def _live_stages(r: dict) -> dict:
            """저장된 stages를 베이스로 현재 파일시스템 상태로 보강.
            특히 wiki는 파이프라인 종료 후 비동기로 진행되므로 파일 위치로 갱신.

            Wiki 판정 (2026-05-18 수정):
            - WIKI_DIR에 stem.md가 있으면 ok (raw→processed 이동 버그 비의존)
            - 보조: PROCESSED_DIR에 stem.txt 있으면 ok (호환)
            - RAW_DIR에 대기 중이면 pending
            """
            s = dict(r.get("stages") or {})
            for k, _ in _STAGE_LABELS:
                s.setdefault(k, "skip" if r.get("ok") is None else "pending")
            ws = r.get("ws", "")
            stem = Path(r.get("name", "")).stem
            # Wiki 상태 갱신 — *진짜 산출물*인 WIKI_DIR의 .md 존재로 판정 (1순위)
            if stem in _wiki_md_stem_set:
                s["wiki"] = "ok"
            elif (PROCESSED_DIR / f"{stem}.txt").exists():
                s["wiki"] = "ok"
            elif (RAW_DIR / ws / f"{stem}.txt").exists() and s.get("wiki") in (None, "pending", "skip"):
                s["wiki"] = "pending"
            return s

        for idx, r in enumerate(list(st.session_state.pipeline_results)):
            name = r["name"]
            with st.container(border=True):
                if r.get("ok") is None:
                    head_cols = st.columns([6, 1])
                    head_cols[0].markdown(f"**⚠️ {name} — 중복 (건너뜀)**")
                    if head_cols[1].button("🗑️", key=f"del_{idx}_{name}"):
                        st.session_state.pipeline_results.pop(idx)
                        save_pipeline_results(st.session_state.pipeline_results)
                        st.rerun()
                    continue

                head_cols = st.columns([6, 1])
                head_cols[0].markdown(f"**{'✅' if r['ok'] else '❌'} {name}**  ·  `{r.get('ws','')}`")
                if head_cols[1].button("🗑️", key=f"del_{idx}_{name}", help="이 항목만 결과 목록에서 제거 (실제 파일은 유지)"):
                    st.session_state.pipeline_results.pop(idx)
                    save_pipeline_results(st.session_state.pipeline_results)
                    st.rerun()

                # 7단계 체크리스트
                s = _live_stages(r)
                stage_cols = st.columns(len(_STAGE_LABELS))
                for col, (key, label) in zip(stage_cols, _STAGE_LABELS):
                    val = s.get(key, "pending")
                    col.markdown(f"{_STAGE_ICONS.get(val, '·')} {label}")

                # 파일 열기 버튼 행
                pdf_p = Path(r["pdf_path"]) if r.get("pdf_path") else None
                txt_p = Path(r["txt_path"]) if r.get("txt_path") else None
                md_p  = Path(r["md_path"])  if r.get("md_path")  else None
                bil_p = Path(r["bilingual_path"]) if r.get("bilingual_path") else None

                btn_cols = st.columns(4)
                if pdf_p and pdf_p.exists():
                    if btn_cols[0].button("📄 PDF", key=f"open_pdf_{idx}_{name}"):
                        open_path(pdf_p, reveal=True)
                if txt_p and txt_p.exists():
                    if btn_cols[1].button("📝 TXT", key=f"open_txt_{idx}_{name}"):
                        open_path(txt_p)
                if md_p and md_p.exists():
                    if btn_cols[2].button("📜 MD", key=f"open_md_{idx}_{name}"):
                        open_path(md_p)
                if bil_p and bil_p.exists():
                    if btn_cols[3].button("📘 번역본", key=f"open_bil_{idx}_{name}"):
                        open_path(bil_p)
                    st.caption(f"📘 번역본: `{bil_p.name}` ({bil_p.stat().st_size // 1024} KB)")

        if st.button("🗑️ 결과 지우기", use_container_width=True):
            st.session_state.pipeline_results = []
            save_pipeline_results([])
            st.rerun()

# ── 탭 2: 처리 기록 ──────────────────────────────────────
with tab_history:
    # 토글: 0KB 항목 숨김
    hide_zero = st.toggle("⚠️ 0KB(빈 파일) 숨김", value=True, key="history_hide_zero")
    rows = []
    row_paths: list[Path] = []          # 행 선택 → 파일/폴더 열기용 (2026-06-11)
    zero_count = 0
    for f in sorted((f for f in DONE_DIR.rglob("*") if f.is_file()), key=lambda x: x.stat().st_mtime, reverse=True):
        sz = f.stat().st_size
        if sz == 0: zero_count += 1
        if sz == 0 and hide_zero: continue
        status = "⚠️ 0KB" if sz == 0 else "✅ 완료"
        rows.append({
            "파일명": f.name,
            "시각": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
            "상태": status,
            "크기": f"{sz // 1024} KB" if sz > 0 else "0 KB ⚠️",
            "경로": str(f.parent.relative_to(DONE_DIR)),
        })
        row_paths.append(f)
    for f in sorted(FAILED_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        sz = f.stat().st_size
        rows.append({
            "파일명": f.name,
            "시각": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
            "상태": "❌ 실패",
            "크기": f"{sz // 1024} KB",
            "경로": "failed",
        })
        row_paths.append(f)
    if zero_count:
        st.caption(
            f"⚠️ 0KB 파일 **{zero_count}건** 감지됨 "
            f"({'숨김' if hide_zero else '표시'} — 처리 중 흔적, 완료 아님)"
        )
    if rows:
        st.caption("ℹ️ 파일명을 클릭하면 파일이 열리고, 📁 경로를 클릭하면 폴더가 열립니다.")
        with st.container(height=700, border=True):
            for _i, (row, fp) in enumerate(zip(rows[:200], row_paths[:200])):
                c_name, c_meta, c_path = st.columns([4.6, 1.6, 2.0])
                _icon = row["상태"].split()[0]
                if c_name.button(f"{_icon} {row['파일명']}", key=f"hist_open_{_i}",
                                 help="클릭하면 파일 열기", use_container_width=True):
                    open_path(fp)
                c_meta.markdown(f"<small>{row['시각']}<br>{row['크기']}</small>",
                                unsafe_allow_html=True)
                if c_path.button(f"📁 {row['경로']}", key=f"hist_folder_{_i}",
                                 help="폴더 열기", use_container_width=True):
                    open_path(fp, reveal=True)
            if len(rows) > 200:
                st.caption(f"… 외 {len(rows) - 200}개 (최신 200개만 표시)")
    else:
        st.info("처리된 파일이 없습니다.")

# ── 탭 3: Wiki 목록 ───────────────────────────────────────
with tab_wiki:
    if st.button("📓 옵시디언에서 위키 금고 열기", key="open_obsidian_vault"):
        open_wiki_vault()
        st.caption("⚠️ 옵시디언이 이미 실행 중이었다면 금고가 바로 안 보일 수 있습니다 — 옵시디언을 껐다 켜 주세요.")
    st.caption("ℹ️ 표에서 행을 클릭하면 아래에 Wiki 본문이 표시됩니다.")
    wiki_files = sorted(WIKI_DIR.rglob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)
    wiki_rows = []
    for wf in wiki_files:
        cat = wf.parent.name
        icon = CATEGORY_ICONS.get(cat, "📚")
        wiki_rows.append({
            "카테고리": f"{icon} {cat}",
            "파일명":   wf.stem,
            "생성 시각": datetime.fromtimestamp(wf.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    if wiki_rows:
        wiki_event = st.dataframe(
            pd.DataFrame(wiki_rows), use_container_width=True, hide_index=True,
            on_select="rerun", selection_mode="single-row", key="wiki_list_table",
        )
        sel_rows = wiki_event.selection.rows if wiki_event and hasattr(wiki_event, "selection") else []
        if sel_rows:
            wf = wiki_files[sel_rows[0]]
            st.divider()
            st.markdown(f"### 📖 {wf.stem}")
            st.caption(f"경로: `{wf}` · {wf.stat().st_size // 1024}KB")
            wc1, wc2 = st.columns(2)
            _wvaults = [v for v in list_obsidian_vaults() if v]
            _in_vault = next(
                (v for v in _wvaults
                 if wf.resolve().is_relative_to(Path(v).expanduser().resolve())),
                None,
            )
            if _in_vault:
                from urllib.parse import quote as _uq
                _vname = Path(_in_vault).expanduser().resolve().name
                _rel_wf = wf.resolve().relative_to(Path(_in_vault).expanduser().resolve())
                _obs_uri = f"obsidian://open?vault={_uq(_vname)}&file={_uq(str(_rel_wf))}"
                if wc1.button("📓 Obsidian에서 열기", type="primary",
                              use_container_width=True, key="wiki_open_obsidian"):
                    if sys.platform == "darwin":
                        subprocess.run(["open", _obs_uri])
                    else:
                        import webbrowser
                        webbrowser.open(_obs_uri)
            else:
                if wc1.button("📄 파일 열기", use_container_width=True, key="wiki_open_file"):
                    open_path(wf)
            if wc2.button("📁 폴더에서 보기", use_container_width=True, key="wiki_open_folder"):
                open_path(wf, reveal=True)

            # ── 다른 금고로 복사 (2026-06-11) ──
            _other_vaults = [
                v for v in list_obsidian_vaults()
                if v and Path(v).expanduser().resolve() != WIKI_DIR.resolve()
            ]
            if _other_vaults:
                with st.expander("📤 다른 금고로 복사"):
                    _cp_target = st.selectbox("대상 금고", _other_vaults, key="wiki_copy_target")
                    _cp_over = st.checkbox("이미 있으면 덮어쓰기", value=False, key="wiki_copy_overwrite")
                    cpc1, cpc2 = st.columns(2)
                    if cpc1.button("📤 이 노트 복사", use_container_width=True, key="wiki_copy_one"):
                        _rel = wf.relative_to(WIKI_DIR)        # 카테고리 하위 구조 보존
                        _dst = Path(_cp_target) / _rel
                        if _dst.exists() and not _cp_over:
                            st.warning(f"대상에 이미 있음: `{_dst}` — 덮어쓰려면 체크 후 다시.")
                        else:
                            try:
                                _dst.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(wf, _dst)
                                append_log(f"위키 노트 복사: {_rel} → {_cp_target}")
                                st.success(f"복사됨: `{_dst}`")
                            except Exception as e:
                                st.error(f"복사 실패: {type(e).__name__} {str(e)[:150]}")
                    if cpc2.button(f"📦 전체 {len(wiki_files)}개 복사", use_container_width=True,
                                   key="wiki_copy_all",
                                   help="현재 금고의 모든 노트를 카테고리 구조 그대로 대상 금고에 복사"):
                        _ok = _skip = _err = 0
                        for _wf2 in wiki_files:
                            _dst = Path(_cp_target) / _wf2.relative_to(WIKI_DIR)
                            if _dst.exists() and not _cp_over:
                                _skip += 1
                                continue
                            try:
                                _dst.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(_wf2, _dst)
                                _ok += 1
                            except Exception:
                                _err += 1
                        append_log(f"위키 노트 전체 복사 → {_cp_target}: 복사 {_ok}·건너뜀 {_skip}·실패 {_err}")
                        st.success(f"복사 {_ok}개 · 이미 있어 건너뜀 {_skip}개"
                                   + (f" · 실패 {_err}개" if _err else ""))
            try:
                content = wf.read_text(encoding="utf-8", errors="ignore")
                with st.container(height=600, border=True):
                    st.markdown(content)
            except Exception as e:
                st.error(f"읽기 실패: {e}")
    else:
        st.info("생성된 Wiki 페이지가 없습니다.")

# ── 탭 4: 실패 파일 ──────────────────────────────────────
with tab_failed:
    st.subheader(f"⚠️ 실패 파일 ({len(_failed_files)})")
    if not _failed_files:
        st.info("실패 파일 없음. 파이프라인 실패 시 자동으로 여기 모입니다.")
    else:
        st.caption(
            "✓ 체크한 파일을 *재시도*하면 **임시 업로드 폴더로 이동 + 📤 파일 업로드 탭에 자동 합산**됩니다. "
            "업로드 탭으로 가서 🚀 파이프라인 실행을 누르세요."
        )

        if "retry_pending_files" not in st.session_state:
            st.session_state.retry_pending_files = []

        # 선택 상태 수집
        selected_paths = [
            p for p in _failed_files
            if st.session_state.get(f"failed_sel_{p}", False)
        ]
        sel_n = len(selected_paths)

        col_retry, col_del = st.columns(2)
        if col_retry.button(
            f"↩️ 선택 {sel_n}건 재시도" if sel_n else "↩️ 선택 재시도",
            type="primary", use_container_width=True, disabled=(sel_n == 0),
            help="UPLOAD_TMP로 이동 + 업로드 탭에 자동 합산",
        ):
            moved = 0
            for p in selected_paths:
                target = UPLOAD_TMP / p.name
                try:
                    shutil.move(str(p), str(target))
                    st.session_state.retry_pending_files.append(str(target))
                    moved += 1
                except Exception as e:
                    st.error(f"{p.name}: {e}")
            st.success(f"{moved}개 파일 재시도 대기 — '📤 파일 업로드' 탭으로 이동해 🚀 누르세요")
            st.rerun()

        if col_del.button(
            f"🗑️ 선택 {sel_n}건 삭제" if sel_n else "🗑️ 선택 삭제",
            use_container_width=True, disabled=(sel_n == 0),
            help="실패 파일 영구 삭제",
        ):
            removed = 0
            for p in selected_paths:
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
            st.success(f"{removed}개 삭제")
            st.rerun()

        st.divider()
        with st.container(height=700, border=True):
            for ff in _failed_files[:200]:
                cols = st.columns([0.5, 4.7, 0.9, 0.9, 0.9])
                sel_key = f"failed_sel_{ff}"
                cols[0].checkbox("선택", key=sel_key, label_visibility="collapsed")
                try:
                    size_kb = ff.stat().st_size // 1024
                    mtime = datetime.fromtimestamp(ff.stat().st_mtime).strftime("%m-%d %H:%M")
                except Exception:
                    size_kb, mtime = 0, ""
                cols[1].markdown(
                    f"**{ff.name}**\n\n<small>{size_kb}KB · {mtime}</small>",
                    unsafe_allow_html=True,
                )
                if cols[2].button("📂", key=f"open_single_{ff}", help="폴더에서 보기"):
                    open_path(ff, reveal=True)
                if cols[3].button("↩️", key=f"retry_single_{ff}", help="이 파일만 재시도"):
                    target = UPLOAD_TMP / ff.name
                    try:
                        shutil.move(str(ff), str(target))
                        st.session_state.retry_pending_files.append(str(target))
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
                if cols[4].button("🗑️", key=f"del_single_{ff}", help="삭제"):
                    try:
                        ff.unlink()
                    except Exception:
                        pass
                    st.rerun()
            if len(_failed_files) > 200:
                st.caption(f"… 외 {len(_failed_files) - 200}개")


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

    for _prov, _info in llm.PROVIDERS.items():
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
    st.markdown("**Claude CLI (구독)** — API 키 대신 Claude 구독으로 사용")
    if llm.claude_cli_available():
        st.success(f"✅ 감지됨: `{llm.claude_cli_path()}` — 구독 로그인 상태면 키 없이 번역에 사용 가능")
    else:
        st.info("미설치. Claude Code를 설치·로그인하면 구독으로 쓸 수 있습니다. (또는 위에서 Anthropic API 키 입력)")

    # ── 위키 저장 폴더(옵시디언 금고) 선택 (2026-06-11) ──
    st.divider()
    # ── 번역 목표 언어 + OCR 언어 (2026-06-13 v0.4.1 선교지 다국어) ──
    st.subheader("🌍 번역 언어")
    st.caption(
        "원문 언어는 자동 감지됩니다. **목표 언어**만 선택하세요.  \n"
        "예) 태국어 원서→한국어, 영어 신학서→베트남어, 크메르어 설교→한국어"
    )
    _cur_tgt = target_lang()

    # 현재 선택 언어가 속한 지역 찾기
    _cur_region = next(
        (r for r, codes in LANG_REGIONS.items() if _cur_tgt in codes),
        list(LANG_REGIONS.keys())[0]
    )
    _rl1, _rl2 = st.columns([1, 2])
    _sel_region = _rl1.selectbox(
        "지역",
        list(LANG_REGIONS.keys()),
        index=list(LANG_REGIONS.keys()).index(_cur_region),
    )
    _region_codes = LANG_REGIONS[_sel_region]
    _def_idx = _region_codes.index(_cur_tgt) if _cur_tgt in _region_codes else 0
    _tgt_sel = _rl2.selectbox(
        "번역 목표 언어",
        _region_codes,
        index=_def_idx,
        format_func=lambda c: f"{LANGS[c][0]}",
        help="문서가 이미 목표 언어면 번역을 건너뜁니다. "
             "라틴 문자 기반 언어(영어·독일어 등)는 자동 스킵 불가 — 원문이 같은 언어인 PDF는 번역 토글을 끄세요.",
    )
    if _tgt_sel != _cur_tgt:
        llm.set_pref("target_lang", _tgt_sel)
        st.success(f"목표 언어 저장: {LANGS[_tgt_sel][0]} — 다음 업로드부터 적용")

    with st.expander("🔬 OCR 언어 설정 (스캔 PDF 문자인식)", expanded=False):
        st.caption(
            "디지털(텍스트 레이어 있는) PDF는 OCR 불필요 — 스캔 이미지 PDF일 때만 영향.  \n"
            "**맥 Vision 언어 코드**: `th-TH`, `km-KH`, `vi-VN`, `id-ID`, `my-MM`, `hi-IN`, `ar-SA`, `ru-RU`  \n"
            "**EasyOCR 코드**: `th`, `vi`, `id`, `ms`, `my`, `hi`, `bn`, `ar`, `ru`, `en`  \n"
            "※ 크메르어(km)·라오어(lo)·암하라어(am) 등은 EasyOCR 미지원 — 맥(Vision)에서 처리하세요."
        )
        _oc1, _oc2 = st.columns(2)
        _om = _oc1.text_input("OCR 언어 (맥, Vision 형식)",
                              value=(llm.get_pref("ocr_langs_mac") or "ko-KR,en-US"),
                              help="쉼표 구분. 예: th-TH,en-US")
        _ow = _oc2.text_input("OCR 언어 (윈도우, EasyOCR 형식)",
                              value=(llm.get_pref("ocr_langs_other") or "ko,en"),
                              help="쉼표 구분. 예: th,en")
        if _om.strip() and _om.strip() != (llm.get_pref("ocr_langs_mac") or "ko-KR,en-US"):
            llm.set_pref("ocr_langs_mac", _om.strip())
        if _ow.strip() and _ow.strip() != (llm.get_pref("ocr_langs_other") or "ko,en"):
            llm.set_pref("ocr_langs_other", _ow.strip())

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
