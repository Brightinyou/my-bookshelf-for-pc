#!/usr/bin/env python3
"""My Bookshelf — PDF→Wiki 파이프라인 (Streamlit GUI)"""

import json
import os
import hashlib
from difflib import SequenceMatcher
import shutil
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import pandas as pd
import streamlit as st

import config as cfg
import llm_providers as llm
from version import APP_VERSION

# ── 처리 로직 서비스 (2026-07-03 pipeline_app.py에서 분리) ──
# UI 코드가 기존 이름 그대로 쓰도록 명시적으로 재노출한다.
from services import wiki as wiki_svc
from services.common import (
    DEFAULT_WS, MD_SUB, PAUSE_DIR, PDF_SUB, TRANS_SUB, TXT_SUB,
    _PathAsUpload, _nfc, _save_json_atomic, append_log, is_paused,
    load_pipeline_results, notify, open_path, pause_flag_path, read_log,
    save_pipeline_results, set_paused,
)
from services.files import (
    _bilingual_candidates, _ko_block_count, _move_unassigned_to_ws,
    _parse_bilingual_block, _save_bilingual_atomic, _save_en_ko_split,
    collect_cross_ws_cache, find_bilingual, find_cross_ws_bilingual,
    find_md, find_pdf, find_split_mds, find_txt, md_dir, processed_stems,
    translated_dir, txt_dir,
)
from services.pipeline_queue import (
    queue_add, queue_clear, queue_list, queue_remove,
)
from services.convert import _do_ocr_only, pdf_to_txt
from services.translate import (
    _needs_translation, _paragraph_already_target, _split_paragraphs_robust,
    _translate_paragraph, _translation_is_valid, build_translate_system,
    engine_label, find_sequential_footnotes, find_skip_section_paragraphs,
    is_english, needs_translation, should_drop_paragraph,
    should_skip_translation, target_lang, translate, translate_engine_options,
    translate_one_chapter,
)
from services.chapters import (
    _is_small_document_for_whole_translation, _merge_chapter_folder,
    _write_single_chapter_from_text, chapters_dir, list_done_books,
    find_overview_file, list_summary_files, load_overview_file,
    load_summary_file, split_book_to_chapters, summarize_book_overview,
    summarize_one_chapter, summary_file_for, SPLIT_MODE_LABELS,
)
from services.papers import (
    download_paper_source, prepare_downloaded_paper_source,
    translate_downloaded_paper,
)
from services.wiki import (
    build_single_chapter_wiki, build_wiki_from_chapter_summaries,
    check_wiki_orphans, ensure_obsidian_vault, list_obsidian_vaults,
    open_wiki_vault, set_wiki_dir, wiki_generator_running,
)
from services.i18n import get_lang, set_lang, t, tf

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
LOG_FILE      = cfg.LOG_FILE
RESULTS_FILE  = cfg.RESULTS_FILE

from services import migrate as _migrate
_migrate.ensure_layout()   # v0.9.0 폴더 재구성 — 옛 데이터 자동 이동 (1회)
for _d in [cfg.UPLOAD_TMP, cfg.PDF_DIR, cfg.TXT_DIR, cfg.CHAPTERS_DIR,
           FAILED_DIR, WIKI_DIR, LOG_FILE.parent, RESULTS_FILE.parent]:
    _d.mkdir(parents=True, exist_ok=True)

CATEGORY_ICONS: dict[str, str] = {}  # 워크스페이스 이름 → 이모지. 빈 경우 기본 📚 사용

import re as _re

# ── UI 래퍼: 세션에서 고른 보관함(Vault)을 위키 서비스에 전달 ──────
def trigger_gemini_wiki(txt_path: Path) -> bool:
    return wiki_svc.trigger_gemini_wiki(txt_path, st.session_state.get("wiki_target_dir"))


def trigger_wiki_generation() -> int:
    return wiki_svc.trigger_wiki_generation(st.session_state.get("wiki_target_dir"))


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
        if dest.exists():
            pdf_save_dir = cfg.PDF_DIR
            pdf_save_dir.mkdir(parents=True, exist_ok=True)
            final_pdf = pdf_save_dir / uf.name
            shutil.move(str(dest), str(final_pdf))
        # TXT·MD → DONE
        _src_txt = txt_path if (txt_path and txt_path.exists()) else None
        md_ok = bool(md_src and md_src.exists())
        if md_ok:
            cfg.TXT_DIR.mkdir(parents=True, exist_ok=True)
            md_dir(DONE_DIR, ws_name).mkdir(parents=True, exist_ok=True)
            if _src_txt:
                final_txt = cfg.TXT_DIR / _src_txt.name
                shutil.move(str(_src_txt), str(final_txt))
            final_md = md_dir(DONE_DIR, ws_name) / md_src.name
            shutil.move(str(md_src), str(final_md))
        elif _src_txt:
            final_txt = cfg.TXT_DIR / _src_txt.name
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


# ── UI ────────────────────────────────────────────────────

def _find_app_icon(name: str) -> Path | None:
    """MyBookshelf.iconset/<name>을 여러 후보 위치에서 찾는다.
    - 개발 트리: core/ 의 부모(레포 루트)
    - .app 번들: Resources/ (pipeline_app.py와 같은 폴더)
    - SSD 실행본: pipeline_app.py와 같은 폴더"""
    here = Path(__file__).resolve().parent
    for base in (here.parent, here, here.parent / "platform" / "windows"):
        p = base / "MyBookshelf.iconset" / name
        if p.exists():
            return p
    return None

_icon_path = _find_app_icon("icon_32x32.png")
_page_icon = str(_icon_path) if _icon_path else "📚"
st.set_page_config(page_title="My Bookshelf", page_icon=_page_icon, layout="wide")

# Cmd/Ctrl+C(복사) 시 뜨던 'Clear caches' 개발자 대화상자는 client.toolbarMode="minimal"
# (.streamlit/config.toml + 실행 플래그)로 개발자 툴바·단축키를 끄면서 제거된다. (2026-07-10)

if "ui_font_scale" not in st.session_state:
    st.session_state["ui_font_scale"] = 1.0

def _font_scale_controls():
    cur = float(st.session_state.get("ui_font_scale", 1.0))
    c1, c2, c3 = st.columns([0.75, 1, 0.75])
    if c1.button("", icon=":material/text_decrease:", key="font_size_minus", use_container_width=True, help="글자 크기 줄이기"):
        st.session_state["ui_font_scale"] = max(0.85, round(cur - 0.05, 2))
        st.rerun()
    c2.markdown(
        f"<div style='text-align:center;color:#6b7280;font-size:0.82rem;line-height:2.35'>"
        f"{int(cur * 100)}%</div>",
        unsafe_allow_html=True,
    )
    if c3.button("", icon=":material/text_increase:", key="font_size_plus", use_container_width=True, help="글자 크기 키우기"):
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

.stage-nav-link {
    display: block;
    width: 100%;
    text-align: center;
    padding: 10px 12px;
    border-radius: 9px;
    border: 1px solid rgba(0, 0, 0, 0.12);
    background: #ffffff;
    color: #4b5563 !important;
    text-decoration: none !important;
    font-weight: 600;
    line-height: 1.15;
    transition: border-color 0.15s ease, background 0.15s ease, color 0.15s ease;
}
.stage-nav-link:hover {
    border-color: rgba(0, 0, 0, 0.28);
    color: #111827 !important;
}
.stage-nav-link.active {
    background: #111827;
    border-color: #111827;
    color: #ffffff !important;
}
/* 버튼 아이콘·라벨 통일 (Material 아이콘 도입, 2026-07-09) */
.stButton button p, .stFormSubmitButton button p { font-weight: 600; }
.stButton button [data-testid="stIconMaterial"],
.stFormSubmitButton button [data-testid="stIconMaterial"] {
    font-size: 1.15em;
    margin-right: 0.15em;
    vertical-align: middle;
}
/* 파일 업로드 영역 강조 — 실제 투입 지점이 눈에 띄도록 (2026-07-10) */
[data-testid="stFileUploaderDropzone"] {
    border: 2px dashed #111827 !important;
    background: #f4f5f7 !important;
    border-radius: 12px !important;
    padding: 1.1rem !important;
}
[data-testid="stFileUploaderDropzone"]:hover {
    background: #eceef1 !important;
    border-color: #000 !important;
}
[data-testid="stFileUploaderDropzone"] [data-testid="stFileUploaderDropzoneInstructions"] svg {
    color: #111827 !important; fill: #111827 !important;
}
@media (prefers-color-scheme: dark) {
  [data-testid="stFileUploaderDropzone"] {
      border-color: #e5e7eb !important; background: rgba(255,255,255,0.04) !important;
  }
}
/* 체크박스 무채색 — 검은 네모 박스 안 체크 (초록 강조색 제거, 2026-07-10) */
[data-testid="stCheckbox"] input:checked + div,
[data-baseweb="checkbox"] input:checked + span,
[data-baseweb="checkbox"] input:checked ~ div {
    background-color: #111827 !important;
    border-color: #111827 !important;
}
[data-testid="stCheckbox"] [data-baseweb="checkbox"] > label > div:first-child {
    border-radius: 4px !important;
    border-color: #6b7280 !important;
}
/* 버튼 아이콘 무채색 고정 */
.stButton button [data-testid="stIconMaterial"],
.stFormSubmitButton button [data-testid="stIconMaterial"] { color: inherit !important; }
/* 커스텀 HTML(내비·메뉴)용 Material Symbols 아이콘 — 이모지 대신 무채색 통일 (2026-07-10) */
.msr {
    font-family: 'Material Symbols Rounded';
    font-weight: normal; font-style: normal;
    font-size: 1.05em; line-height: 1;
    letter-spacing: normal; text-transform: none; white-space: nowrap;
    vertical-align: -0.15em; margin-right: 0.35em;
    font-feature-settings: 'liga'; -webkit-font-feature-settings: 'liga';
    -webkit-font-smoothing: antialiased;
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
st.caption(t("PDF → TXT변환 → 장별 분할 → 영문번역 → 요약생성 → Obsidian Wiki"))

_loading_step("파일 목록 확인 중…", "처리된 파일과 API 설정을 읽고 있습니다")

# ── 상태 배너 ────────────────────────────────────────────
_avail_api_providers = [llm.PROVIDERS[p]["label"] for p in llm.API_PROVIDERS if llm.has_key(p)]
_avail_cli_providers = [llm.PROVIDERS[p]["label"] for p in llm.CLI_PROVIDERS if llm.has_key(p)]
_avail_ai_providers = _avail_api_providers + _avail_cli_providers
_wiki_key_ok = bool(_avail_ai_providers)
wg_ok = wiki_generator_running()
_status_spacer, col_s1, col_s2, col_s3, col_s4 = st.columns([2.8, 1.1, 1.1, 1.1, 1.1])
col_s1.metric(t("API 키"), tf("%d개", len(_avail_api_providers)) if _avail_api_providers else t("❌ 없음"))
col_s2.metric(t("CLI 구독"), tf("%d개", len(_avail_cli_providers)) if _avail_cli_providers else t("없음"))
col_s3.metric(t("위키 생성기"), t("🔄 생성 중") if wg_ok else t("대기"))
col_s4.metric(t("Wiki 완성"), sum(1 for _ in WIKI_DIR.rglob("*.md")))
if not _avail_ai_providers:
    st.error(t("⚠️ 사용 가능한 AI가 없습니다 — ⚙️ 설정 탭에서 API 키를 입력하거나 CLI 구독 도구를 활성화하세요."))

# ── 초기 메뉴 ─────────────────────────────────────────────
# 탭 → Material Symbols 아이콘 이름 (내비·메뉴·제목 공통, 무채색 통일, 2026-07-10)
_STAGE_ICONS = {
    "menu": "grid_view", "1_txt": "description", "2_split": "content_cut",
    "3_translate": "translate", "4_summary": "summarize", "5_wiki": "menu_book",
    "settings": "settings", "all_run": "rocket_launch",
}
TASKS = [
    ("1_txt", "텍스트 변환", "PDF/TXT를 텍스트로 변환 · 업로드 대기 → 변환 TXT"),
    ("2_split", "챕터 분할", "책 TXT를 챕터 단위로 분리 · 변환 TXT → chapters"),
    ("3_translate", "영문번역", "챕터를 한국어로 번역 · chapters → 번역본(_ko.txt)"),
    ("4_summary", "문서요약", "챕터별 요약 노트 생성 · chapters → 요약(_wiki.md)"),
    ("5_wiki", "위키반영", "요약을 Obsidian 노트로 저장 · 요약(_wiki.md) → 보관함(Vault)"),
    ("settings", "설정", "API 키와 위키 생성 모델 설정"),
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
    st.markdown(t("#### 작업 메뉴"))
    st.info(t(
        "처음 사용 전 확인: 이 앱은 사용자가 제공한 PDF/TXT를 정리, 번역, 요약, 위키 노트로 재구성하는 개인 작업 도구입니다. "
        "원문 저작권과 이용허락은 사용자 책임으로 확인해야 하며, 외부 AI/CLI로 전송되는 텍스트에는 민감정보나 배포 권한이 불분명한 내용을 넣지 마세요."
    ))
    for _tid, _title, _desc in TASKS:
        _clicked = st.query_params.get("view") == _tid
        _mico = f'<span class="msr" style="font-size:1.2rem">{_STAGE_ICONS.get(_tid, "")}</span>'
        st.markdown(
            f'<a class="menu-card" href="?view={_tid}" target="_self">'
            f'<span class="menu-title">{_mico}{t(_title)}</span>'
            f'<span class="menu-desc">{t(_desc)}</span>'
            f'</a>',
            unsafe_allow_html=True,
        )
        if _clicked:
            st.session_state["active_view"] = _tid
            st.query_params.clear()
            st.rerun()
    _loading_ph.empty()
    st.session_state["_app_loaded"] = True
    st.stop()

_STAGE_TASKS = [
    ("menu", "메뉴"),
    ("1_txt", "텍스트 변환"),
    ("2_split", "챕터 분할"),
    ("3_translate", "영문번역"),
    ("4_summary", "문서요약"),
    ("5_wiki", "위키반영"),
    ("settings", "설정"),
]
# 처리 중(잠금)에는 탭 이동 링크를 비활성 텍스트로 렌더 — 작업 이탈 방지 (2026-07-09)
_run_lock = st.session_state.get("_run_lock")
_nav_cols = st.columns(len(_STAGE_TASKS))
for _col, (_tid, _label) in zip(_nav_cols, _STAGE_TASKS):
    _active_cls = " active" if _active_view == _tid else ""
    _ico = f'<span class="msr">{_STAGE_ICONS.get(_tid, "")}</span>'
    with _col:
        if _run_lock:
            st.markdown(
                f'<span class="stage-nav-link{_active_cls}" '
                f'style="opacity:0.4;pointer-events:none;cursor:not-allowed">{_ico}{t(_label)}</span>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<a class="stage-nav-link{_active_cls}" href="?view={_tid}" target="_self">{_ico}{t(_label)}</a>',
                unsafe_allow_html=True,
            )
if st.query_params.get("view") in {tid for tid, _ in _STAGE_TASKS}:
    _view = st.query_params.get("view")
    if _view != _active_view:
        if _view == "menu":
            st.session_state.pop("active_view", None)
        else:
            st.session_state["active_view"] = _view
        st.query_params.clear()
        st.rerun()

with st.expander(t("📁 저장 위치"), expanded=False):
    _loc_rows = [
        ("0_업로드대기", cfg.UPLOAD_TMP),
        ("1_원본PDF", cfg.PDF_DIR),
        ("2_변환TXT", cfg.TXT_DIR),
        ("3_챕터", cfg.CHAPTERS_DIR),
        ("위키(Vault)", WIKI_DIR),
        ("실패", FAILED_DIR),
        ("로그", cfg.LOG_DIR),
        ("구버전보관", cfg.LEGACY_KEEP),
    ]
    for _lname, _lpath in _loc_rows:
        _lc1, _lc2 = st.columns([0.85, 2.2])
        _lc1.markdown(f"**{_lname}**")
        _lc2.caption(str(_lpath))
        if _lc1.button(t("열기"), icon=":material/folder_open:", key=f"open_loc_{_lname}", use_container_width=True, disabled=not _lpath.exists()):
            open_path(_lpath)



# ─── 공용 헬퍼 ───────────────────────────────────────────


def _view_target_from_item(it: dict) -> Path | None:
    obj = it.get("obj")
    if isinstance(obj, Path):
        return obj
    if isinstance(obj, tuple) and obj and isinstance(obj[0], Path):
        ko_path = obj[0].with_name(obj[0].stem + "_ko.txt")
        return ko_path if ko_path.exists() else obj[0]
    if hasattr(obj, "_p"):
        return Path(obj._p)
    if isinstance(obj, dict):
        if isinstance(obj.get("txt"), Path):
            return obj["txt"]
        stem = obj.get("stem")
        ws = obj.get("ws") or DEFAULT_WS
        if stem:
            txt_path = cfg.TXT_DIR / f"{stem}.txt"
            ch_path = chapters_dir(ws, stem)
            if txt_path.exists():
                return txt_path
            if ch_path.exists():
                return ch_path
    if isinstance(obj, str):
        rel_path = cfg.BASE_DIR / obj
        if rel_path.exists():
            return rel_path
    return None


def _goto_view(view_id: str) -> None:
    st.session_state["active_view"] = view_id
    st.query_params.clear()
    st.rerun()


def _set_stage_completion(title: str, message: str, next_stage: str | None = None,
                          open_target: Path | None = None) -> None:
    st.session_state["_stage_completion"] = {
        "title": title,
        "message": message,
        "next_stage": next_stage,
        "open_target": str(open_target) if open_target else "",
    }


def _clear_stage_completion() -> None:
    st.session_state.pop("_stage_completion", None)


def _render_stage_completion_notice() -> None:
    payload = st.session_state.get("_stage_completion")
    if not payload:
        return

    def _render_body():
        st.success(payload["title"])
        st.write(payload["message"])
        c1, c2, c3 = st.columns(3)
        if payload.get("next_stage"):
            if c1.button(t("다음 단계"), icon=":material/arrow_forward:", key="stage_done_next", use_container_width=True, type="primary"):
                next_stage = payload["next_stage"]
                _clear_stage_completion()
                _goto_view(next_stage)
        if payload.get("open_target"):
            _target = Path(payload["open_target"])
            if c2.button(t("결과 폴더 열기"), icon=":material/folder_open:", key="stage_done_open", use_container_width=True):
                open_path(_target, reveal=_target.is_file())
        if c3.button(t("닫기"), icon=":material/close:", key="stage_done_close", use_container_width=True):
            _clear_stage_completion()
            st.rerun()

    if hasattr(st, "dialog"):
        @st.dialog(t("완료"))
        def _stage_completion_dialog():
            _render_body()
        _stage_completion_dialog()
    else:
        with st.container(border=True):
            _render_body()


def _stage_folder(stage_id: str) -> Path:
    if stage_id == "1_txt":
        return cfg.TXT_DIR
    if stage_id in {"2_split", "3_translate", "4_summary"}:
        return cfg.CHAPTERS_DIR
    if stage_id == "5_wiki":
        return WIKI_DIR
    return cfg.BASE_DIR


def _chapter_rel_paths(ws_name: str, stem: str) -> list[str]:
    ch_dir = chapters_dir(ws_name, stem)
    if not ch_dir.exists():
        return []
    return [
        str(f.relative_to(cfg.BASE_DIR))
        for f in sorted(ch_dir.glob("??_*.txt"))
        if not f.stem.endswith(("_ko", "_wiki"))
    ]


def _dismiss_split_nosplit(stem: str) -> None:
    pending = st.session_state.get("split2_nosplit", [])
    if isinstance(pending, list) and stem in pending:
        st.session_state["split2_nosplit"] = [item for item in pending if item != stem]


def _queue_book_chapters_for_next_stage(ws_name: str, stem: str) -> list[str]:
    chapter_rels = _chapter_rel_paths(ws_name, stem)
    if not chapter_rels:
        return []
    if _needs_translation(stem):
        queue_add("tab3_ready", chapter_rels)
    else:
        queue_add("tab4_ready", chapter_rels)
    return chapter_rels


def _save_book_as_single_chapter(ws_name: str, stem: str) -> tuple[bool, str, list[str]]:
    existing_rels = _chapter_rel_paths(ws_name, stem)
    if existing_rels:
        queue_remove("tab2_ready", [stem])
        _dismiss_split_nosplit(stem)
        _queue_book_chapters_for_next_stage(ws_name, stem)
        return True, t("기존 장 파일을 다시 사용했습니다."), existing_rels

    txt_path = find_txt(DONE_DIR, ws_name, stem)
    md_path = find_md(DONE_DIR, ws_name, stem)
    source_path = txt_path or md_path
    if source_path is None:
        return False, t("TXT/MD 파일이 없습니다."), []

    source_text = source_path.read_text(encoding="utf-8", errors="ignore")
    if not source_text.strip():
        return False, t("TXT/MD 내용이 비어 있습니다."), []

    ch_path, _ = _write_single_chapter_from_text(ws_name, stem, source_text)
    queue_remove("tab2_ready", [stem])
    _dismiss_split_nosplit(stem)
    chapter_rels = _queue_book_chapters_for_next_stage(ws_name, stem)
    if not chapter_rels:
        return False, t("단일장 파일 생성에 실패했습니다."), []
    return True, ch_path.name, chapter_rels


def _upload_token(upload_name: str, upload_bytes: bytes) -> str:
    digest = hashlib.sha1(upload_bytes).hexdigest()[:12]
    return f"{Path(upload_name).name}:{len(upload_bytes)}:{digest}"


def _copy_direct_upload_to_processing(stage_name: str, upload_name: str, upload_bytes: bytes) -> tuple[Path, str]:
    token = _upload_token(upload_name, upload_bytes)
    digest = token.rsplit(":", 1)[-1]
    staging_dir = UPLOAD_TMP / "_direct_uploads" / stage_name
    staging_dir.mkdir(parents=True, exist_ok=True)
    raw_name = Path(upload_name).name
    staging_path = staging_dir / raw_name
    if staging_path.exists():
        try:
            if staging_path.read_bytes() != upload_bytes:
                staging_path = staging_dir / f"{Path(upload_name).stem}__{digest}{Path(upload_name).suffix or '.txt'}"
        except Exception:
            staging_path = staging_dir / f"{Path(upload_name).stem}__{digest}{Path(upload_name).suffix or '.txt'}"
    staging_path.write_bytes(upload_bytes)
    return staging_path, token


def _prepare_uploaded_single_chapter(ws_name: str, upload_name: str, upload_bytes: bytes, stage_name: str) -> tuple[bool, Path | None, str, str]:
    _copy_direct_upload_to_processing(stage_name, upload_name, upload_bytes)
    stem = _nfc(Path(upload_name).stem)
    suffix = ".txt"
    src_dir = cfg.TXT_DIR
    src_dir.mkdir(parents=True, exist_ok=True)
    src_path = src_dir / f"{stem}{suffix}"
    src_path.write_bytes(upload_bytes)
    source_text = src_path.read_text(encoding="utf-8", errors="ignore")
    if not source_text.strip():
        return False, None, stem, t("TXT 내용이 비어 있습니다.")

    existing_rels = _chapter_rel_paths(ws_name, stem)
    if len(existing_rels) > 1:
        return False, None, stem, t("이미 여러 장으로 분할된 책입니다. 2-장별분할 탭에서 처리하세요.")
    if existing_rels:
        existing_path = cfg.BASE_DIR / existing_rels[0]
        existing_text = existing_path.read_text(encoding="utf-8", errors="ignore")
        if existing_text == source_text:
            return True, existing_path, stem, t("기존 단일장 파일을 이어서 사용합니다.")

    ch_path, _ = _write_single_chapter_from_text(ws_name, stem, source_text)
    return True, ch_path, stem, t("단일장 파일을 저장했습니다.")


def _count_files(path: Path, patterns: list[str], exclude_suffixes: tuple = ()) -> int:
    """폴더에서 패턴에 맞는 파일 수. exclude_suffixes는 stem 끝 필터 (_ko 등)."""
    if not path.exists():
        return 0
    n = 0
    for pat in patterns:
        for f in path.glob(pat):
            if f.is_file() and not (exclude_suffixes and f.stem.endswith(exclude_suffixes)):
                n += 1
    return n


def _chapter_counts() -> tuple[int, int, int]:
    """chapters/ 전체의 (원문 챕터, 번역본 _ko, 요약 _wiki.md/.json) 개수."""
    root = cfg.CHAPTERS_DIR
    src_n = ko_n = 0
    summary_stems: set[str] = set()
    if root.exists():
        for f in root.rglob("??_*.txt"):
            if f.stem.endswith("_ko"):
                ko_n += 1
            elif not f.stem.endswith("_wiki"):
                src_n += 1
        for f in root.rglob("*_wiki.md"):
            summary_stems.add(str(f.with_suffix("")))
        for f in root.rglob("*_wiki.json"):
            summary_stems.add(str(f.with_suffix("")))
    return src_n, ko_n, len(summary_stems)


def _stage_flow_panel(app_title: str, app_desc: str,
                      cards: list[tuple[str, Path, str]], key_prefix: str) -> None:
    """앱 헤더 + (작게) 진행 요약·폴더 열기. 실제 작업 공간이 눈에 띄도록
    폴더 열기란은 접이식으로 작게 처리한다 (2026-07-09). cards=[(라벨, 경로, 개수문구)]"""
    st.markdown(f"### {t(app_title)}")
    st.caption(t(app_desc))
    _summary = "  ·  ".join(f"{t(label)} {count_txt}" for label, _p, count_txt in cards)
    st.caption(_summary)
    with st.expander(t("📁 폴더 열기"), expanded=False):
        _fcols = st.columns(len(cards))
        for i, (label, path, _count_txt) in enumerate(cards):
            if _fcols[i].button(t(label), icon=":material/folder_open:", key=f"{key_prefix}_open_{i}",
                                use_container_width=True, disabled=not path.exists(),
                                help=str(path)):
                open_path(path)
    st.divider()


# ─── 처리 잠금(시작/중단) 런 패널 (2026-07-09) ──────────────────
# 시작 → 처리 화면만 표시(다른 위젯 미렌더 + 상단 탭 이동 잠금).
# 항목 1개 처리 후 st.rerun → 다음 항목. 중단 클릭은 다음 rerun에서 감지돼
# 현재 항목 처리 후 멈춘다(남은 항목은 지속 큐에 남아 재시작 시 이어짐).

def _run_active(tab: str) -> bool:
    return bool(st.session_state.get(f"{tab}_running"))


def _run_start(tab: str, work: list) -> None:
    """선택한 작업 목록으로 처리 시작. work=처리기 인자 목록."""
    if not work:
        return
    st.session_state[f"{tab}_running"] = True
    st.session_state[f"{tab}_queue"] = list(work)
    st.session_state[f"{tab}_total"] = len(work)
    st.session_state[f"{tab}_log"] = []
    st.session_state["_run_lock"] = tab
    st.rerun()


def _run_finish(tab: str) -> None:
    st.session_state[f"{tab}_running"] = False
    if st.session_state.get("_run_lock") == tab:
        st.session_state.pop("_run_lock", None)


def _run_panel(tab: str, title: str, process_one, on_done=None) -> None:
    """처리 화면 렌더 + 항목 1개 처리 + rerun. process_one(item)->(ok, msg 문자열).
    on_done(): 큐 소진 시 1회 실행(전체요약 등 후처리)."""
    queue = list(st.session_state.get(f"{tab}_queue", []))
    total = st.session_state.get(f"{tab}_total", len(queue)) or 1
    done = total - len(queue)
    log = list(st.session_state.get(f"{tab}_log", []))

    st.markdown(f"### ⏳ {t(title)}")
    st.progress(min(done / total, 1.0), text=tf("%d/%d 처리 중", done, total))
    _stopped = st.button(t("중단"), icon=":material/stop:", key=f"{tab}_stopbtn", type="primary")
    st.caption(t("처리 중에는 다른 기능이 잠깁니다. '중단'을 누르면 현재 항목까지 마친 뒤 멈추고, 남은 작업은 다시 '시작'으로 이어집니다."))
    with st.container(height=300, border=True):
        for _ln in log[-80:]:
            st.markdown(_ln)

    if _stopped:
        _run_finish(tab)
        st.rerun()
    if not queue:
        _run_finish(tab)
        if on_done:
            try:
                on_done()
            except Exception as _e:
                st.warning(str(_e)[:200])
        st.rerun()

    _item = queue[0]
    try:
        _ok, _msg = process_one(_item)
    except Exception as _e:
        _ok, _msg = False, f"{type(_e).__name__}: {str(_e)[:150]}"
    log.append(f"{'✅' if _ok else '❌'} {_msg}")
    st.session_state[f"{tab}_log"] = log
    st.session_state[f"{tab}_queue"] = queue[1:]
    st.rerun()


_DND_HINT = "📎 파일 선택 또는 이 영역으로 끌어다 놓기(Drag & Drop) 가능"


def _current_wiki_dir() -> Path:
    target = (st.session_state.get("wiki5_active_dir") or "").strip()
    if target:
        return Path(target)
    try:
        data = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8")) if cfg.CONFIG_FILE.exists() else {}
        target = str(data.get("dirs", {}).get("wiki", "")).strip()
        if target:
            return Path(target).expanduser()
    except Exception:
        pass
    return WIKI_DIR


_render_stage_completion_notice()


def _checklist_keys(items: list[dict], prefix: str) -> list[str]:
    """항목별 위젯 키. 같은 key(예: 동일 stem의 .txt/.md 공존)가 있으면
    뒤쪽에 __N을 붙여 StreamlitDuplicateElementKey 크래시를 막는다. (2026-07-06)"""
    keys, seen = [], {}
    for it in items:
        k = f"{prefix}_{it['key']}"
        n = seen.get(k, 0)
        seen[k] = n + 1
        keys.append(k if n == 0 else f"{k}__{n}")
    return keys


def _checklist(items: list[dict], prefix: str, height: int = 320, viewable: bool = False) -> list:
    """체크박스 파일 목록. items=[{"key":str,"label":str,"meta":str,"obj":any}]
    Returns: 선택된 obj 목록."""
    _keys = _checklist_keys(items, prefix)
    h1, h2, h3 = st.columns([1.3, 1, 4])
    if h1.button(t("전체 선택"), icon=":material/select_all:", key=f"{prefix}_sa", use_container_width=True):
        for _k in _keys:
            st.session_state[_k] = True
        st.rerun()
    if h2.button(t("해제"), icon=":material/deselect:", key=f"{prefix}_da", use_container_width=True):
        for _k in _keys:
            st.session_state[_k] = False
        st.rerun()
    h3.caption(tf("총 %d개", len(items)))
    selected = []
    with st.container(height=height, border=True):
        for idx, it in enumerate(items):
            k = _keys[idx]
            cols = st.columns([0.05, 0.82, 0.13]) if viewable else st.columns([0.05, 0.95])
            c1, c2 = cols[0], cols[1]
            chk = c1.checkbox(" ", key=k, label_visibility="collapsed")
            c2.markdown(
                f"**{it['label']}** &nbsp;<small style='color:#9ca3af'>{it['meta']}</small>",
                unsafe_allow_html=True,
            )
            if viewable:
                target = _view_target_from_item(it)
                safe_key = _re.sub(r"[^a-zA-Z0-9가-힣_-]+", "_", str(it["key"]))[:80]
                if cols[2].button(t("보기"), icon=":material/visibility:", key=f"{prefix}_view_{idx}_{safe_key}", use_container_width=True,
                                  disabled=target is None):
                    open_path(target, reveal=target.is_file())
            if chk:
                selected.append(it["obj"])
    return selected


def _translate_engine_radio(label: str, key: str) -> str:
    _avail = [(eid, lbl) for eid, lbl, av, _ in translate_engine_options() if av]
    _ids = [eid for eid, _lbl in _avail]
    _labels = [lbl for _eid, lbl in _avail]
    _wp, _wm = llm.wiki_provider_model()
    _wiki_engine = f"{_wp}:{_wm}" if _wp and _wm else ""
    _pref = llm.get_pref("translate_engine", "")
    _default = _wiki_engine if _wiki_engine in _ids else (_pref if _pref in _ids else (_ids[0] if _ids else ""))
    _idx = _ids.index(_default) if _default in _ids else 0
    _sel = st.radio(t(label), _labels, index=_idx, horizontal=True, key=key)
    _engine = _ids[_labels.index(_sel)]
    if _engine != _pref:
        llm.set_pref("translate_engine", _engine)
    return _engine


def _wiki_model_radio(key: str) -> tuple[str, str]:
    """사용 가능한 AI 모델 radio 선택기. (prov, model) 반환.
    선택이 현재 wiki_provider_model과 다르면 자동으로 set_wiki_model 호출."""
    _avail = [(p, m)
              for p, info in llm.PROVIDERS.items()
              if llm.has_key(p)
              for m in info["models"]]
    if not _avail:
        st.warning(t("사용 가능한 AI 없음 — ⚙️ 설정 탭에서 API 키를 입력하세요."))
        return llm.wiki_provider_model()
    _wp, _wm = llm.wiki_provider_model()
    _labels = [f"{llm.PROVIDERS[p]['label']} · {m}" for p, m in _avail]
    _cur = f"{llm.PROVIDERS.get(_wp, {}).get('label', _wp)} · {_wm}"
    _idx = _labels.index(_cur) if _cur in _labels else 0
    _sel = st.radio(t("🤖 AI 모델"), _labels, index=_idx, horizontal=True, key=key)
    _p, _m = _avail[_labels.index(_sel)]
    if (_p, _m) != (_wp, _wm):
        llm.set_wiki_model(_p, _m)
    return _p, _m


def _settings_ai_label() -> str:
    """설정에서 선택된 AI(공급자·모델)의 사람용 라벨."""
    _wp, _wm = llm.wiki_provider_model()
    _plabel = llm.PROVIDERS.get(_wp, {}).get("label", _wp)
    return f"{_plabel} · {_wm}"


def _settings_engine_id() -> str:
    """설정에서 선택된 AI의 번역 엔진 id (provider:model)."""
    _wp, _wm = llm.wiki_provider_model()
    return f"{_wp}:{_wm}" if _wp and _wm else ""


def _settings_ai_note() -> None:
    """AI 모델은 설정에서만 고른다는 안내 + 현재 선택 표시 (탭 공통)."""
    st.caption(t("🤖 AI 모델은 ⚙️ 설정에서 선택합니다 · 현재: ") + _settings_ai_label())


_loading_step("화면 구성 중…", "탭과 UI를 초기화하고 있습니다")

# ── 1: TXT변환 / 전체 실행 ───────────────────────────────
if _active_view in {"1_txt", "all_run"}:
    _pdf_dir1 = cfg.PDF_DIR
    _stage_flow_panel(
        ":material/description: 텍스트 변환",
        "PDF의 텍스트 레이어를 추출해 TXT로 저장합니다 (OCR 변환된 문서만 가능).",
        [
            ("① 처리전 · 업로드 대기", UPLOAD_TMP,
             tf("%d개 대기", _count_files(UPLOAD_TMP, ['*.pdf', '*.txt', '*.md']))),
            ("② 처리후 · 변환 TXT", cfg.TXT_DIR,
             tf("%d권 변환됨", _count_files(cfg.TXT_DIR, ['*.txt']))),
            ("📄 원본 PDF 보관", _pdf_dir1,
             tf("%d개 보관", _count_files(_pdf_dir1, ['*.pdf']))),
        ],
        "flow1",
    )

    _ws1 = DEFAULT_WS
    _fast1 = True

    # 파일 업로드
    _uploads1 = st.file_uploader(
        t("PDF 또는 TXT 업로드 (여러 파일 가능)"),
        type=["pdf", "txt", "md"], accept_multiple_files=True, key="ocr_uploader",
    )
    st.caption(t(_DND_HINT))
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
            st.success(tf("📥 처리 대기 목록에 추가됨: %s", ", ".join(_added1)))
            st.rerun()  # 대기 목록 갱신 (세션스테이트로 중복 저장 방지됨)

    with st.expander(t("🔎 논문 출처로 가져오기"), expanded=False):
        _paper_src1 = st.text_input(
            t("논문 출처"),
            key="ocr1_paper_source",
            placeholder=t("URL, DOI(10.xxxx/...), doi:..., arXiv 번호 또는 arxiv.org 링크"),
        )
        st.caption(t(
            "💡 URL이 잘 안 될 때: ① 로그인·구독이 필요한 페이지(대학도서관·유료 저널)나 "
            "본문이 아닌 소개 페이지 링크는 받아올 수 없습니다 — PDF를 내려받아 위에서 직접 업로드하세요. "
            "② DOI(10.xxxx/…)나 arXiv 번호(예: 2412.12107)가 있으면 그 값을 넣는 편이 가장 안정적입니다. "
            "③ 링크 끝이 `.pdf`인 직접 주소를 쓰세요. ④ 그래도 안 되면 브라우저에서 PDF를 저장한 뒤 업로드하는 방법이 가장 확실합니다."
        ))
        if st.button(t("다운로드 확인 후 TXT 저장"), icon=":material/download:", key="ocr1_source_prepare",
                     use_container_width=True, type="primary",
                     disabled=not _paper_src1.strip()):
            _ok_prep1 = False
            with st.status(t("논문 출처 확인 중…"), expanded=True):
                _ok_dl1, _src_file1, _reason1 = download_paper_source(_paper_src1)
                if not _ok_dl1 or not _src_file1:
                    st.error(tf("(%s) 때문에 가져올 수 없습니다.", _reason1))
                else:
                    st.write(tf("✅ 다운로드 가능: `%s`", _src_file1.name))
                    _ok_prep1, _final_txt1, _final_pdf1, _msg_prep1 = prepare_downloaded_paper_source(_src_file1)
                    if _ok_prep1:
                        st.success(tf("✅ TXT 저장 완료: %s", _msg_prep1))
                        if _final_pdf1:
                            st.write(tf("📄 원본 PDF 보관: `%s`", _final_pdf1))
                    else:
                        st.error(tf("(%s) 때문에 TXT로 저장할 수 없습니다.", _msg_prep1))
            if _ok_dl1 and _src_file1 and _ok_prep1:
                # rerun 후에도 TXT/PDF 위치를 열어볼 수 있게 세션에 보존
                st.session_state["paper1_result"] = {
                    "name": _src_file1.name,
                    "txt": str(_final_txt1) if _final_txt1 else "",
                    "pdf": str(_final_pdf1) if _final_pdf1 else "",
                }
                _src_file1.unlink(missing_ok=True)   # Temp에 받은 원본 정리 (보관본은 pdf/에 복사됨)
                st.rerun()

    _pr1 = st.session_state.get("paper1_result")
    if _pr1:
        with st.container(border=True):
            _prh1, _prh2 = st.columns([5, 1])
            _prh1.markdown(tf("**🔎 최근 가져온 논문:** %s", _pr1["name"]))
            if _prh2.button(t("닫기"), icon=":material/close:", key="paper1_result_close", use_container_width=True):
                st.session_state.pop("paper1_result", None)
                st.rerun()
            _txt_p1 = Path(_pr1["txt"]) if _pr1.get("txt") else None
            _pdf_p1 = Path(_pr1["pdf"]) if _pr1.get("pdf") else None
            _pra1, _prb1 = st.columns([4.2, 1])
            _pra1.caption(tf("📝 변환 TXT: %s", _txt_p1 if _txt_p1 else "—"))
            if _prb1.button(t("위치 열기"), icon=":material/folder_open:", key="paper1_open_txt", use_container_width=True,
                            disabled=not (_txt_p1 and _txt_p1.exists())):
                open_path(_txt_p1, reveal=True)
            _pra2, _prb2 = st.columns([4.2, 1])
            _pra2.caption(tf("📄 원본 PDF: %s", _pdf_p1 if _pdf_p1 else t("— (TXT 출처라 PDF 없음)")))
            if _prb2.button(t("위치 열기"), icon=":material/folder_open:", key="paper1_open_pdf", use_container_width=True,
                            disabled=not (_pdf_p1 and _pdf_p1.exists())):
                open_path(_pdf_p1, reveal=True)

    st.divider()

    # 처리 대기 목록 (UPLOAD_TMP)
    _pending_all1 = sorted(
        [f for f in UPLOAD_TMP.glob("*") if f.is_file() and f.suffix.lower() in {".pdf",".txt",".md"}]
        if UPLOAD_TMP.exists() else [],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    st.markdown(tf("#### 처리 대기 (%d개)", len(_pending_all1)))
    if _pending_all1:
        _items1 = [
            {"key": f.name,
             "label": f.name,
             "meta": f"{f.stat().st_size//1024}KB · {datetime.fromtimestamp(f.stat().st_mtime).strftime('%m-%d %H:%M')}",
             "obj": _PathAsUpload(f)}
            for f in _pending_all1
        ]
        _sel1 = _checklist(_items1, "ocr1", height=250, viewable=True)
        _b1c1, _b1c2 = st.columns(2)
        _run_sel1 = _b1c1.button(tf("텍스트 변환 처리 (%d개)", len(_sel1)), icon=":material/play_arrow:", key="ocr1_run_sel",
                                   use_container_width=True, type="primary", disabled=len(_sel1)==0)
        _del1 = _b1c2.button(tf("삭제 (%d개)", len(_sel1)), icon=":material/delete:", key="ocr1_del_sel",
                             use_container_width=True, disabled=len(_sel1)==0)
        if _del1 and _sel1:
            for _dobj1 in _sel1:
                try:
                    Path(_dobj1._p).unlink(missing_ok=True)
                except Exception:
                    pass
            st.session_state.pop("_ocr_queued", None)
            st.rerun()
        _to_run1 = _sel1 if _run_sel1 else []
        if _to_run1:
            _prog1 = st.progress(0.0)
            _done_txt_paths1: list[Path] = []
            for _i1, _uf1 in enumerate(_to_run1, 1):
                with st.status(f"텍스트 변환 [{_i1}/{len(_to_run1)}]: {_uf1.name}", expanded=False):
                    _r1 = _do_ocr_only(_uf1, _ws1, fast=_fast1)
                if _r1["ok"] and _r1.get("txt_path"):
                    _done_txt_paths1.append(Path(_r1["txt_path"]))
                (st.success if _r1["ok"] else st.error)(
                    f"{'✅' if _r1['ok'] else '❌'} {_uf1.name}" +
                    (f" → `{Path(_r1['txt_path']).name}`" if _r1["ok"] else f": {_r1['error']}")
                )
                _prog1.progress(_i1 / len(_to_run1))
            if _done_txt_paths1:
                _set_stage_completion(
                    t("1-TXT변환 완료"),
                    tf("%d개 파일 처리를 마쳤습니다. 다음 단계에서 장별 분할을 진행하세요.", len(_done_txt_paths1)),
                    next_stage="2_split",
                    open_target=_stage_folder("1_txt"),
                )
            st.session_state.pop("_ocr_queued", None)  # 처리 완료 후 큐 초기화
            st.rerun()
    else:
        st.info(t("대기 중인 파일 없음 — 위에서 PDF를 업로드하세요."))

    st.divider()

    # 실패 기록
    _fail1 = sorted([p for p in FAILED_DIR.rglob("*") if p.is_file()],
                    key=lambda p: p.stat().st_mtime, reverse=True) if FAILED_DIR.exists() else []
    if _fail1:
        with st.expander(tf("⚠️ 실패 %d건", len(_fail1))):
            for _ff1 in _fail1[:30]:
                _fc1, _fc2, _fc3 = st.columns([5, 1, 1])
                _fc1.caption(_ff1.name)
                if _fc2.button("", icon=":material/undo:", key=f"retry_f1_{_ff1}", help="재시도"):
                    shutil.move(str(_ff1), str(UPLOAD_TMP / _ff1.name)); st.rerun()
                if _fc3.button("", icon=":material/delete:", key=f"del_f1_{_ff1}", help="삭제"):
                    try: _ff1.unlink()
                    except Exception: pass
                    st.rerun()

    st.info(t("💡 다음 단계: **📂 챕터 분할**으로 이동하세요"))


# ── 2: 장별 분할 ────────────────────────────────────────
if _active_view == "2_split":
    _split_arch_dir2 = cfg.TXT_ARCHIVE_DIR

    def _archive_split_source(stem: str) -> bool:
        """분할이 끝난 원본 TXT/MD를 1_txt/완료/로 이동 (2026-07-07)."""
        moved = False
        _split_arch_dir2.mkdir(parents=True, exist_ok=True)
        for _ext in (".txt", ".md"):
            _srcf = cfg.TXT_DIR / (stem + _ext)
            if _srcf.exists():
                try:
                    shutil.move(str(_srcf), str(_split_arch_dir2 / _srcf.name))
                    moved = True
                except Exception:
                    pass
        return moved

    def _proc_split2(obj):
        _ws, _stem = obj["ws"], obj["stem"]
        _sn, _serr, _smode = split_book_to_chapters(_ws, _stem)
        if _serr:
            return False, f"{_stem}: {_serr}"
        _cdir = chapters_dir(_ws, _stem)
        _new = [str(f.relative_to(cfg.BASE_DIR)) for f in sorted(_cdir.glob("??_*.txt"))
                if not f.stem.endswith(("_ko", "_wiki"))]
        if not _new:
            return False, f"{_stem}: 챕터 생성 안 됨"
        queue_remove("tab2_ready", [_stem])
        if _needs_translation(_stem):
            st.session_state["split2_any_en"] = True
            queue_add("tab3_ready", _new)
        else:
            queue_add("tab4_ready", _new)
        _archive_split_source(_stem)
        return True, f"{_stem} → {len(_new)}챕터 ({t(SPLIT_MODE_LABELS.get(_smode, _smode))})"

    def _split2_on_done():
        _any_en = st.session_state.pop("split2_any_en", False)
        _set_stage_completion(
            t("2-챕터 분할 완료"),
            t("분할을 마쳤습니다.")
            + (" " + t("영문 책 → 영문번역") if _any_en else " " + t("한글 책 → 문서요약")),
            next_stage="3_translate" if _any_en else "4_summary",
            open_target=_stage_folder("2_split"),
        )

    if _run_active("split2"):
        _run_panel("split2", "챕터 분할 처리 중", _proc_split2, on_done=_split2_on_done)
        st.stop()

    _ch_root2f = cfg.CHAPTERS_DIR
    _n_books2f = len([d for d in _ch_root2f.iterdir() if d.is_dir()]) if _ch_root2f.exists() else 0
    _stage_flow_panel(
        ":material/content_cut: 챕터 분할",
        "책 TXT를 챕터(Chapter) 단위 파일로 분리해 책별 폴더에 저장합니다.",
        [
            ("① 처리전 · 변환 TXT", cfg.TXT_DIR,
             tf("%d권", _count_files(cfg.TXT_DIR, ['*.txt', '*.md']))),
            ("② 처리후 · 챕터 폴더", _ch_root2f, tf("%d권 분할됨", _n_books2f)),
            ("✅ 완료 보관 (원본 TXT)", cfg.TXT_ARCHIVE_DIR,
             tf("%d권 보관", _count_files(cfg.TXT_ARCHIVE_DIR, ['*.txt', '*.md']))),
        ],
        "flow2",
    )
    _sp_prov2, _sp_model2 = llm.wiki_provider_model()
    _settings_ai_note()

    # TXT 직접 업로드
    _up2 = st.file_uploader(t("TXT 직접 업로드"),
                              type=["txt", "md"], accept_multiple_files=True, key="split_uploader")
    st.caption(t(_DND_HINT))
    if _up2:
        _added_split_stems2: list[str] = []
        for _u2 in _up2:
            cfg.TXT_DIR.mkdir(parents=True, exist_ok=True)
            _dst2 = cfg.TXT_DIR / _u2.name
            _dst2.write_bytes(_u2.read())
            _added_split_stems2.append(_nfc(Path(_u2.name).stem))
        if _added_split_stems2:
            queue_add("tab2_ready", _added_split_stems2)
        st.success(tf("%d개 TXT 저장 완료", len(_up2))); st.rerun()

    # ── 분할 대기 (큐 기반 + 1_txt/ 전체 폴백) ──────────────
    _q2_stems = queue_list("tab2_ready")
    _split_pend2: list[dict] = []
    _split_done2: list[dict] = []
    _split_short2: list[dict] = []
    _txt_root2 = cfg.TXT_DIR

    # 큐에 없어도 1_txt/에 있는 TXT 모두 포함
    _all_txt2_stems = ({f.stem for f in _txt_root2.glob("*.txt")} | {f.stem for f in _txt_root2.glob("*.md")}) if _txt_root2.exists() else set()
    _q2_stems_set = set(_q2_stems)
    _extra2 = sorted(_all_txt2_stems - _q2_stems_set)  # 큐에 없는 TXT
    _all2_stems = list(_q2_stems) + _extra2

    for _stem2 in _all2_stems:
        _txt2 = _txt_root2 / (_stem2 + ".txt")
        if not _txt2.exists():
            _txt2 = _txt_root2 / (_stem2 + ".md")
        if not _txt2.exists():
            continue
        _ch2 = chapters_dir(DEFAULT_WS, _stem2)
        _ch_txts2 = [f for f in (_ch2.glob("??_*.txt") if _ch2.exists() else [])
                     if not f.stem.endswith(("_ko", "_wiki"))]
        _meta2 = f"{_txt2.stat().st_size//1024}KB" + ("" if _stem2 in _q2_stems_set else " ·미등록")
        if _ch_txts2:
            _split_done2.append({"stem": _stem2, "n": len(_ch_txts2), "ch_dir": _ch2})
        else:
            _src2 = _txt2.read_text(encoding="utf-8", errors="ignore")
            _item2 = {"key": _stem2, "label": _stem2, "meta": _meta2,
                      "obj": {"ws": DEFAULT_WS, "stem": _stem2}}
            if _is_small_document_for_whole_translation(_src2):
                _item2["text"] = _src2
                _split_short2.append(_item2)
            else:
                _split_pend2.append(_item2)

    st.markdown(tf("#### 분할 대기 (%d권)", len(_split_pend2)))
    if _split_pend2:
        _sel2 = _checklist(_split_pend2, "split2", height=280, viewable=True)
        _b2c1, _b2c2, _b2c3 = st.columns(3)
        _rs2 = _b2c1.button(tf("분할 처리 (%d권)", len(_sel2)), icon=":material/play_arrow:", key="split2_run_sel",
                              use_container_width=True, type="primary", disabled=len(_sel2)==0)
        _next2 = _b2c2.button(tf("다음단계로 이동 (%d권)", len(_sel2)), icon=":material/arrow_forward:", key="split2_next",
                              use_container_width=True, disabled=len(_sel2)==0,
                              help=t("분할 없이 단일장으로 저장하고 영문은 영문번역, 한글은 문서요약으로 이동"))
        _del2 = _b2c3.button(tf("삭제 (%d권)", len(_sel2)), icon=":material/delete:", key="split2_del",
                             use_container_width=True, disabled=len(_sel2)==0)
        if _del2 and _sel2:
            for _dobj2 in _sel2:
                _dstem2 = _dobj2["stem"]
                for _dext2 in (".txt", ".md"):
                    try:
                        (_txt_root2 / (_dstem2 + _dext2)).unlink(missing_ok=True)
                    except Exception:
                        pass
                queue_remove("tab2_ready", [_dstem2])
            st.rerun()
        if _next2 and _sel2:
            # 다음단계로 이동: 분할 없이 단일장 저장 후 라우팅 (빠른 처리라 즉시)
            _completed2 = 0
            _queued_translate2 = 0
            _queued_summary2 = 0
            for _s2 in _sel2:
                _ok2, _detail2, _new_chs2 = _save_book_as_single_chapter(_s2["ws"], _s2["stem"])
                if _ok2 and _new_chs2:
                    _completed2 += 1
                    if _needs_translation(_s2["stem"]):
                        _queued_translate2 += 1
                    else:
                        _queued_summary2 += 1
                else:
                    st.warning(f"⚠️ {_s2['stem']}: {_detail2}")
            if _completed2:
                _next_stage2 = "3_translate" if _queued_translate2 else "4_summary"
                _set_stage_completion(
                    t("2-단일장 저장 완료"),
                    tf("%d건을 다음 단계로 보냈습니다.", _completed2)
                    + (" " + t("영문 → 영문번역") if _queued_translate2 else " " + t("한글 → 문서요약")),
                    next_stage=_next_stage2,
                    open_target=_stage_folder("2_split"),
                )
                st.rerun()
        if _rs2 and _sel2:
            _run_start("split2", _sel2)
    else:
        if _split_short2:
            st.warning(t("⚠️ 짧은 문서가 감지되었습니다. 아래 '짧은 문서 확인'에서 분할 처리 또는 다음단계로 이동을 선택하세요."))
        else:
            st.info(t("분할 대기 없음 — 📄 텍스트 변환에서 TXT를 먼저 생성하거나 아래에서 수동 추가하세요"))

    if _split_short2:
        st.divider()
        st.markdown(tf("### ⚠️ 짧은 문서 확인 (%d권)", len(_split_short2)))
        with st.container(border=True):
            st.caption(t("짧은 문서는 챕터로 나누기 애매합니다. 챕터로 분할하거나, 통째로 다음 단계(영문→영문번역·한글→문서요약)로 보낼 수 있습니다."))
            for _sh2 in _split_short2:
                _sc1, _sc2, _sc3, _sc4 = st.columns([4, 1, 1.4, 1.4])
                _sc1.markdown(f"**{_sh2['label']}**")
                _sc2.caption(_sh2["meta"])
                if _sc3.button(t("분할 처리"), icon=":material/play_arrow:", key=f"short_split_yes_{_sh2['key']}",
                               use_container_width=True):
                    _sn2, _serr2, _ = split_book_to_chapters(_sh2["obj"]["ws"], _sh2["obj"]["stem"], allow_short=True)
                    if _serr2:
                        st.warning(f"⚠️ {_sh2['key']}: {_serr2}")
                    else:
                        st.success(f"✅ {_sh2['key']} → {_sn2}개 챕터")
                        queue_remove("tab2_ready", [_sh2["obj"]["stem"]])
                        _ch_dir2 = chapters_dir(_sh2["obj"]["ws"], _sh2["obj"]["stem"])
                        _new_chs2 = [str(f.relative_to(cfg.BASE_DIR))
                                     for f in sorted(_ch_dir2.glob("??_*.txt"))
                                     if not f.stem.endswith(("_ko", "_wiki"))]
                        if _new_chs2:
                            if _needs_translation(_sh2["obj"]["stem"]):
                                queue_add("tab3_ready", _new_chs2)
                            else:
                                queue_add("tab4_ready", _new_chs2)
                            _archive_split_source(_sh2["obj"]["stem"])
                        st.rerun()
                if _sc4.button(t("다음단계로 이동"), icon=":material/arrow_forward:", key=f"short_split_keep_{_sh2['key']}",
                               use_container_width=True, type="primary"):
                    _one_path2, _ = _write_single_chapter_from_text(_sh2["obj"]["ws"], _sh2["obj"]["stem"], _sh2["text"])
                    queue_remove("tab2_ready", [_sh2["obj"]["stem"]])
                    _new_chs2 = [str(f.relative_to(cfg.BASE_DIR))
                                 for f in sorted(_one_path2.parent.glob("??_*.txt"))
                                 if not f.stem.endswith(("_ko", "_wiki"))]
                    _next_stage2s = "3_translate" if _needs_translation(_sh2["obj"]["stem"]) else "4_summary"
                    if _new_chs2:
                        queue_add("tab3_ready" if _next_stage2s == "3_translate" else "tab4_ready", _new_chs2)
                        _archive_split_source(_sh2["obj"]["stem"])
                    _set_stage_completion(
                        t("2-단일장 저장 완료"),
                        tf("%s 을(를) 단일장으로 저장했습니다.", _sh2["label"])
                        + (" " + t("영문 문서 → 영문번역") if _next_stage2s == "3_translate"
                           else " " + t("한글 문서 → 문서요약")),
                        next_stage=_next_stage2s,
                        open_target=_stage_folder("2_split"),
                    )
                    st.rerun()

    # 장 구조 미감지 — 단일장 저장 선택지 (2026-07-03)
    _nosplit2 = st.session_state.get("split2_nosplit", [])
    if _nosplit2:
        st.divider()
        st.markdown(tf("#### 장 구조 미감지 (%d권)", len(_nosplit2)))
        st.caption(t("장 헤딩을 찾지 못한 문서입니다. 통째로 번역·요약하려면 단일장으로 저장하세요."))
        for _ns2 in list(_nosplit2):
            _nc1, _nc2, _nc3 = st.columns([4, 1.6, 0.7])
            _nc1.markdown(f"**{_ns2}**")
            if _nc2.button(t("단일장으로 저장"), icon=":material/article:", key=f"nosplit_save_{_ns2}", use_container_width=True):
                _sn2b, _smsg2b, _ = split_book_to_chapters(DEFAULT_WS, _ns2, allow_short=True)
                if _sn2b > 0:
                    queue_remove("tab2_ready", [_ns2])
                    _ch_dir2b = chapters_dir(DEFAULT_WS, _ns2)
                    _new_chs2b = [str(f.relative_to(cfg.BASE_DIR))
                                  for f in sorted(_ch_dir2b.glob("??_*.txt"))
                                  if not f.stem.endswith(("_ko", "_wiki"))]
                    if _new_chs2b:
                        if _needs_translation(_ns2):
                            queue_add("tab3_ready", _new_chs2b)
                        else:
                            queue_add("tab4_ready", _new_chs2b)
                        _archive_split_source(_ns2)
                    st.session_state["split2_nosplit"] = [x for x in _nosplit2 if x != _ns2]
                    st.rerun()
                else:
                    st.error(f"❌ {_ns2}: {_smsg2b}")
            if _nc3.button("", icon=":material/close:", key=f"nosplit_dismiss_{_ns2}", help="목록에서 제거"):
                st.session_state["split2_nosplit"] = [x for x in _nosplit2 if x != _ns2]
                st.rerun()

    # 수동 추가 expander
    with st.expander(t("➕ 수동으로 추가 (기존 책에서 선택)")):
        _mc2a, _mc2b = st.columns([3, 2])
        _search2 = _mc2a.text_input(t("책 이름 검색"), key="split2_search", placeholder=t("검색어 입력…"))
        _sort2 = _mc2b.radio(t("정렬"), [t("최근 추가순"), t("이름순")], horizontal=True, key="split2_sort")
        _all_txts2 = (list(_txt_root2.glob("*.txt")) + list(_txt_root2.glob("*.md"))) if _txt_root2.exists() else []
        _all_txts2 = sorted(_all_txts2, key=lambda f: f.stat().st_mtime, reverse=True) \
                     if _sort2 == t("최근 추가순") else sorted(_all_txts2, key=lambda f: f.name)
        _filtered2 = [f for f in _all_txts2 if _search2.lower() in f.stem.lower()] \
                     if _search2 else _all_txts2
        _manual_items2 = [{"key": f.name, "label": f.name,
                           "meta": f"{f.stat().st_size//1024}KB", "obj": f.stem}
                          for f in _filtered2]
        _msel2 = _checklist(_manual_items2, "split2m", height=220)
        if st.button(tf("선택 항목 큐에 추가 (%d권)", len(_msel2)), icon=":material/add:", key="split2m_add",
                     disabled=len(_msel2)==0):
            queue_add("tab2_ready", _msel2); st.rerun()

    st.divider()
    st.markdown(tf("#### 분할 완료 (%d권)", len(_split_done2)))
    if _split_done2:
        with st.container(height=200, border=True):
            for _sd2 in _split_done2:
                _sdc1, _sdc2, _sdc3, _sdc4 = st.columns([5, 1.2, 1.2, 1])
                _sdc1.markdown(f"**{_sd2['stem']}** &nbsp;<small style='color:#9ca3af'>{_sd2['n']}챕터</small>",
                               unsafe_allow_html=True)
                if _sdc2.button(t("열기"), icon=":material/folder_open:", key=f"open_ch2_{_sd2['stem']}", use_container_width=True):
                    open_path(_sd2["ch_dir"])
                if _sdc3.button("", icon=":material/merge:", key=f"merge_ch2_{_sd2['stem']}", help="다시 합치기"):
                    _okm2, _mp2, _mm2 = _merge_chapter_folder(DEFAULT_WS, _sd2["stem"], prefer_ko=False)
                    (st.success if _okm2 else st.error)(
                        f"{'✅' if _okm2 else '❌'} {_sd2['stem']}: {Path(_mp2).name if _okm2 else _mm2}")
                    st.rerun()
                if _sdc4.button(t("합친 번역본"), icon=":material/translate:", key=f"merge_ch2_ko_{_sd2['stem']}", use_container_width=True):
                    _okm2, _mp2, _mm2 = _merge_chapter_folder(DEFAULT_WS, _sd2["stem"], prefer_ko=True)
                    (st.success if _okm2 else st.error)(
                        f"{'✅' if _okm2 else '❌'} {_sd2['stem']}: {Path(_mp2).name if _okm2 else _mm2}")
                    st.rerun()
                if st.button("", icon=":material/refresh:", key=f"resplit2_{_sd2['stem']}", help="재분할"):
                    for _f2 in _sd2["ch_dir"].glob("*"):
                        try: _f2.unlink()
                        except Exception: pass
                    st.rerun()
    else:
        st.caption(t("완료된 분할 없음"))

    st.info(t("💡 다음 단계: **🌐 영문번역**으로 이동하세요"))


# ── 3: 번역 ─────────────────────────────────────────────
if _active_view == "3_translate":
    _tr_eng3 = _settings_engine_id()

    def _proc_translate3(rel):
        _cf = cfg.BASE_DIR / rel
        if not _cf.exists():
            return False, f"{Path(rel).name}: 파일 없음"
        _ok, _msg = translate_one_chapter(_cf, _tr_eng3)
        if _ok:
            queue_remove("tab3_ready", [rel])
            queue_add("tab4_ready", [rel])
        return _ok, f"{_cf.name}: {str(_msg)[:80]}"

    def _tr3_on_done():
        _set_stage_completion(
            t("3-영문번역 완료"),
            t("번역을 마쳤습니다. 다음 단계에서 요약을 생성하세요."),
            next_stage="4_summary",
            open_target=_stage_folder("3_translate"),
        )

    if _run_active("tr3"):
        _run_panel("tr3", "영문번역 처리 중", _proc_translate3, on_done=_tr3_on_done)
        st.stop()

    _src_n3f, _ko_n3f, _ = _chapter_counts()
    _ch_root3f = cfg.CHAPTERS_DIR
    _stage_flow_panel(
        ":material/translate: 영문번역",
        "챕터 TXT를 한국어로 번역해 같은 폴더에 `_ko.txt`로 저장합니다.",
        [
            ("① 처리전 · 원문 챕터", _ch_root3f, tf("%d개", _src_n3f)),
            ("② 처리후 · 번역본 (_ko.txt)", _ch_root3f, tf("%d개 번역됨", _ko_n3f)),
        ],
        "flow3",
    )

    if not _tr_eng3:
        st.warning(t("사용 가능한 AI 없음 — ⚙️ 설정 탭에서 API 키를 입력하세요."))
    else:
        _settings_ai_note()

        # TXT 직접 업로드 — 즉시 번역하지 않고 번역 대기 큐에 등록 (2026-07-09)
        _up3 = st.file_uploader(t("TXT 직접 업로드"),
                                  type=["txt"], accept_multiple_files=True, key="tr3_uploader")
        st.caption(t(_DND_HINT))
        st.caption(t("업로드한 TXT는 아래 '번역 대기'에 등록됩니다. [▶ 시작]을 눌러야 번역이 시작됩니다."))
        if not _up3:
            st.session_state.pop("_tr3_uploaded_tokens", None)
        if _up3:
            _seen3 = set(st.session_state.get("_tr3_uploaded_tokens", []))
            _staged3 = 0
            for _u3 in _up3:
                _u3_bytes = _u3.getvalue()
                _token3 = _upload_token(_u3.name, _u3_bytes)
                if _token3 in _seen3:
                    continue
                _seen3.add(_token3)
                _ok3p, _ch3_path, _book3u, _prep3_msg = _prepare_uploaded_single_chapter(
                    DEFAULT_WS, _u3.name, _u3_bytes, "translate"
                )
                if not _ok3p or _ch3_path is None:
                    st.error(f"❌ {_u3.name}: {_prep3_msg}")
                    continue
                queue_add("tab3_ready", [str(_ch3_path.relative_to(cfg.BASE_DIR))])
                _staged3 += 1
            st.session_state["_tr3_uploaded_tokens"] = sorted(_seen3)
            if _staged3:
                st.success(tf("번역 대기에 %d개 등록됨 — 아래에서 [▶ 시작]", _staged3))
            st.rerun()

        # ── 번역 대기 (큐 기반) ──────────────────────────────
        _q3_rels = queue_list("tab3_ready")
        _tr_pend3: list[dict] = []
        _tr_done3 = 0
        for _rel3 in _q3_rels:
            _cf3 = cfg.BASE_DIR / _rel3
            if not _cf3.exists():
                continue
            _ko3 = _cf3.with_name(_cf3.stem + "_ko.txt")
            if _ko3.exists():
                _tr_done3 += 1
            else:
                _meta3 = f"{_cf3.stat().st_size//1024}KB"
                if _cf3.with_name(_cf3.stem + "_ko.progress.json").exists():
                    _meta3 += t(" · ♻️ 중단됨 — 이어하기 가능")
                _tr_pend3.append({
                    "key": _rel3,
                    "label": f"{_cf3.parent.name} / {_cf3.name}",
                    "meta": _meta3,
                    "obj": _rel3,
                })

        st.divider()
        st.markdown(tf("#### 번역 대기 (%d개) / 완료 %d개", len(_tr_pend3), _tr_done3))
        if _tr_pend3:
            _sel3 = _checklist(_tr_pend3, "tr3", height=280, viewable=True)
            _b3c1, _b3c2 = st.columns(2)
            _rs3 = _b3c1.button(tf("시작 (%d개)", len(_sel3)), icon=":material/play_arrow:", key="tr3_start",
                                  use_container_width=True, type="primary", disabled=len(_sel3)==0)
            _del3 = _b3c2.button(tf("삭제 (%d개)", len(_sel3)), icon=":material/delete:", key="tr3_del",
                                 use_container_width=True, disabled=len(_sel3)==0)
            if _del3 and _sel3:
                queue_remove("tab3_ready", _sel3)
                st.rerun()
            if _rs3 and _sel3:
                _run_start("tr3", _sel3)
        else:
            st.info(t("번역 대기 없음 — 📂 챕터 분할에서 챕터를 먼저 분리하세요"))

    st.info(t("💡 다음 단계: **📝 문서요약**으로 이동하세요"))


# ── 4: 요약생성 ─────────────────────────────────────────
if _active_view == "4_summary":
    def _proc_summary4(rel):
        _cf = cfg.BASE_DIR / rel
        if not _cf.exists():
            return False, f"{Path(rel).name}: 파일 없음"
        _book = _nfc(_cf.parent.name)
        _ok, _msg = summarize_one_chapter(_cf, _book)
        if _ok:
            queue_remove("tab4_ready", [rel])
            queue_remove("tab4_failed", [rel])
            _touched = set(st.session_state.get("summ4_touched", []))
            _touched.add(_book)
            st.session_state["summ4_touched"] = sorted(_touched)
            queue_add("tab5_ready", [_book])
        else:
            queue_remove("tab4_ready", [rel])
            queue_add("tab4_failed", [rel])
        return _ok, f"{_cf.name}: {str(_msg)[:70]}"

    def _summ4_on_done():
        for _stem in st.session_state.get("summ4_touched", []):
            try:
                summarize_book_overview(DEFAULT_WS, _stem)
            except Exception:
                pass
        st.session_state.pop("summ4_touched", None)
        _set_stage_completion(
            t("4-문서요약 완료"),
            t("요약을 마쳤습니다. 다음 단계에서 Wiki 반영을 진행하세요."),
            next_stage="5_wiki",
            open_target=_stage_folder("4_summary"),
        )

    if _run_active("summ4"):
        _run_panel("summ4", "문서요약 처리 중", _proc_summary4, on_done=_summ4_on_done)
        st.stop()

    _src_n4f, _ko_n4f, _json_n4f = _chapter_counts()
    _ch_root4f = cfg.CHAPTERS_DIR
    _stage_flow_panel(
        ":material/summarize: 문서요약",
        "챕터 TXT(번역본 우선)로 요약을 생성해 같은 폴더에 `_wiki.md`로 저장합니다.",
        [
            ("① 처리전 · 챕터 (번역본 우선)", _ch_root4f,
             tf("원문 %d · 번역 %d", _src_n4f, _ko_n4f)),
            ("② 처리후 · 요약 (_wiki.md)", _ch_root4f, tf("%d개 요약됨", _json_n4f)),
        ],
        "flow4",
    )

    _prov_ok4 = any(llm.has_key(p) for p in llm.PROVIDERS)
    if not _prov_ok4:
        st.warning(t("요약 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요."))
    else:
        _settings_ai_note()

        # TXT 직접 업로드 — 즉시 요약하지 않고 요약 대기 큐에 등록 (2026-07-09)
        _up4 = st.file_uploader(t("TXT 직접 업로드"),
                                  type=["txt"], accept_multiple_files=True, key="summ4_uploader")
        st.caption(t(_DND_HINT))
        st.caption(t("업로드한 TXT는 아래 '요약 대기'에 등록됩니다. [▶ 시작]을 눌러야 요약이 시작됩니다."))
        if not _up4:
            st.session_state.pop("_summ4_uploaded_tokens", None)
        if _up4:
            _seen4 = set(st.session_state.get("_summ4_uploaded_tokens", []))
            _staged4n = 0
            for _u4 in _up4:
                _u4_bytes = _u4.getvalue()
                _token4 = _upload_token(_u4.name, _u4_bytes)
                if _token4 in _seen4:
                    continue
                _seen4.add(_token4)
                _ok4p, _ch4_path, _book4u, _prep4_msg = _prepare_uploaded_single_chapter(
                    DEFAULT_WS, _u4.name, _u4_bytes, "summary"
                )
                if not _ok4p or _ch4_path is None:
                    st.error(f"❌ {_u4.name}: {_prep4_msg}")
                    continue
                queue_add("tab4_ready", [str(_ch4_path.relative_to(cfg.BASE_DIR))])
                queue_remove("tab4_failed", [str(_ch4_path.relative_to(cfg.BASE_DIR))])
                _staged4n += 1
            st.session_state["_summ4_uploaded_tokens"] = sorted(_seen4)
            if _staged4n:
                st.success(tf("요약 대기에 %d개 등록됨 — 아래에서 [▶ 시작]", _staged4n))
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
            _cf4 = cfg.BASE_DIR / _rel4
            if not _cf4.exists():
                _q4_remove_missing.append(_rel4)
                continue
            _bstem4 = _nfc(_cf4.parent.name)
            if summary_file_for(_cf4) is not None:
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
            _cf4f = cfg.BASE_DIR / _rel4f
            if not _cf4f.exists():
                _q4_remove_missing.append(_rel4f)
                continue
            if summary_file_for(_cf4f) is not None:
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
        st.markdown(tf("#### 요약 대기 (%d개) / 완료 %d개", len(_sum_pend4), _sum_done4))
        if _sum_pend4:
            _sel4 = _checklist(_sum_pend4, "summ4", height=280, viewable=True)
            _b4c1, _b4c2 = st.columns(2)
            _rs4 = _b4c1.button(tf("시작 (%d개)", len(_sel4)), icon=":material/play_arrow:", key="summ4_start",
                                  use_container_width=True, type="primary", disabled=len(_sel4)==0)
            _del4 = _b4c2.button(tf("삭제 (%d개)", len(_sel4)), icon=":material/delete:", key="summ4_del",
                                 use_container_width=True, disabled=len(_sel4)==0)
            _sel4_rels = [str(_cfx.relative_to(cfg.BASE_DIR)) for _cfx, _bx in _sel4]
            if _del4 and _sel4:
                queue_remove("tab4_ready", _sel4_rels)
                queue_remove("tab4_failed", _sel4_rels)
                st.rerun()
            if _rs4 and _sel4_rels:
                _run_start("summ4", _sel4_rels)
        else:
            st.info(t("요약 대기 없음 — 🌐 영문번역 처리 후 자동 등록되거나 위에서 TXT를 직접 업로드하세요"))

        if _sum_failed4:
            st.markdown(tf("#### 요약 실패 (%d개)", len(_sum_failed4)))
            _fail_sel4 = _checklist(_sum_failed4, "summ4_failed", height=180)
            _f4c1, _f4c2 = st.columns([2, 1])
            if _f4c1.button(tf("선택 재시도 대기 (%d개)", len(_fail_sel4)), icon=":material/refresh:", key="summ4_retry_failed",
                              use_container_width=True, disabled=len(_fail_sel4)==0):
                queue_remove("tab4_failed", _fail_sel4)
                queue_add("tab4_ready", _fail_sel4)
                st.rerun()
            if _f4c2.button(t("실패 목록 비우기"), icon=":material/delete_sweep:", key="summ4_clear_failed", use_container_width=True):
                queue_clear("tab4_failed")
                st.rerun()

        # 책 전체요약 관리 (2026-07-07)
        _ch_root4o = cfg.CHAPTERS_DIR
        _ov_books4 = [d for d in (_ch_root4o.iterdir() if _ch_root4o.exists() else [])
                      if d.is_dir() and list_summary_files(d)]
        if _ov_books4:
            with st.expander(tf("📚 책 전체요약 (<책제목>_전체요약.md) — %d권", len(_ov_books4))):
                st.caption(t("장별 요약을 합쳐 만든 책 전체 요약입니다. 위키반영 전에 열어서 고칠 수 있고, 수정본이 허브 노트에 그대로 반영됩니다."))
                for _bd4 in sorted(_ov_books4, key=lambda d: d.name):
                    _ovf4 = find_overview_file(DEFAULT_WS, _nfc(_bd4.name))
                    _has4 = _ovf4 is not None
                    _oc1, _oc2, _oc3, _oc4 = st.columns([4, 1.2, 1.4, 1])
                    _oc1.markdown(f"**{_bd4.name}**")
                    _oc2.caption(t("✅ 있음") if _has4 else t("— 없음"))
                    if _oc3.button(t("재생성") if _has4 else t("생성"),
                                   icon=":material/refresh:" if _has4 else ":material/play_arrow:",
                                   key=f"ov4_gen_{_bd4.name}", use_container_width=True):
                        with st.status(tf("📚 책 전체요약 생성: %s", _bd4.name), expanded=False):
                            _ok_ov4b, _msg_ov4b = summarize_book_overview(DEFAULT_WS, _nfc(_bd4.name))
                        (st.success if _ok_ov4b else st.error)(
                            f"{'✅' if _ok_ov4b else '❌'} {_msg_ov4b[:100]}")
                        if _ok_ov4b:
                            st.rerun()
                    if _oc4.button(t("보기"), icon=":material/visibility:", key=f"ov4_view_{_bd4.name}",
                                   use_container_width=True, disabled=not _has4):
                        open_path(_ovf4, reveal=True)
                    if _has4:
                        _ovd4 = load_overview_file(_ovf4)
                        if _ovd4 and _ovd4.get("summary"):
                            st.caption(f"› {_ovd4['summary'][:110]}")

        # 수동 추가 expander
        with st.expander(t("➕ 수동으로 추가 (기존 챕터에서 선택)")):
            _mc4a, _mc4b = st.columns([3, 2])
            _search4 = _mc4a.text_input(t("책/챕터 이름 검색"), key="summ4_search", placeholder=t("검색어 입력…"))
            _sort4 = _mc4b.radio(t("정렬"), [t("최근 추가순"), t("이름순")], horizontal=True, key="summ4_sort")
            _ch_root4m = cfg.CHAPTERS_DIR
            _all_cfs4 = list(_ch_root4m.rglob("??_*.txt")) if _ch_root4m.exists() else []
            _all_cfs4 = [f for f in _all_cfs4 if not f.stem.endswith(("_ko","_wiki"))]
            _all_cfs4 = sorted(_all_cfs4, key=lambda f: f.stat().st_mtime, reverse=True) \
                        if _sort4 == t("최근 추가순") else sorted(_all_cfs4, key=lambda f: str(f))
            _filt4 = [f for f in _all_cfs4 if _search4.lower() in str(f).lower()] if _search4 else _all_cfs4
            _mitems4 = [{"key": str(f.relative_to(cfg.BASE_DIR)), "label": f"{f.parent.name}/{f.name}",
                         "meta": f"{f.stat().st_size//1024}KB", "obj": str(f.relative_to(cfg.BASE_DIR))}
                        for f in _filt4]
            _msel4 = _checklist(_mitems4, "summ4m", height=200)
            if st.button(tf("선택 항목 큐에 추가 (%d개)", len(_msel4)), icon=":material/add:", key="summ4m_add", disabled=len(_msel4)==0):
                queue_add("tab4_ready", _msel4); st.rerun()

    st.info(t("💡 다음 단계: **📖 위키반영**으로 이동하세요"))


# ── 5: Wiki반영 ─────────────────────────────────────────
if _active_view == "5_wiki":
    _cur_wiki5_path = _current_wiki_dir()

    def _proc_wiki5(stem):
        # 1단계: 챕터별 개별 노트, 2단계: 허브 노트(책 전체요약 + 링크)
        _cdir = chapters_dir(DEFAULT_WS, stem)
        _cfail = 0
        for _cjf in list_summary_files(_cdir):
            _cok, _ = build_single_chapter_wiki(DEFAULT_WS, stem, _cjf, wiki_dir=_cur_wiki5_path)
            if not _cok:
                _cfail += 1
        _ok, _msg = build_wiki_from_chapter_summaries(DEFAULT_WS, stem, wiki_dir=_cur_wiki5_path)
        if _ok:
            queue_remove("tab5_ready", [stem])
        _tail = f" (챕터노트 실패 {_cfail})" if _cfail else ""
        return _ok, f"{stem}: {Path(_msg).name if _ok else str(_msg)[:70]}{_tail}"

    def _wiki5_on_done():
        _set_stage_completion(
            t("5-Wiki 반영 완료"),
            t("Wiki 반영을 마쳤습니다."),
            next_stage=None,
            open_target=_stage_folder("5_wiki"),
        )

    if _run_active("wiki5"):
        _run_panel("wiki5", "위키반영 처리 중", _proc_wiki5, on_done=_wiki5_on_done)
        st.stop()

    _, _, _json_n5f = _chapter_counts()
    _ch_root5f = cfg.CHAPTERS_DIR
    _vault5f = _current_wiki_dir()
    _n_notes5f = sum(1 for _ in _vault5f.rglob("*.md")) if _vault5f.exists() else 0
    _stage_flow_panel(
        ":material/menu_book: 위키반영",
        "챕터 요약(_wiki.md)들을 합쳐 Obsidian 보관함(Vault)에 위키 노트로 저장합니다.",
        [
            ("① 처리전 · 요약 (_wiki.md)", _ch_root5f, tf("%d개", _json_n5f)),
            ("② 처리후 · Obsidian 보관함", _vault5f, tf("%d노트", _n_notes5f)),
        ],
        "flow5",
    )

    # ── 위키 저장 보관함(Vault) 선택 ──────────────────────────────────
    _vaults5 = list_obsidian_vaults()
    # 세션에 저장된 보관함(Vault) 경로가 있으면 우선 사용, 없으면 기본값
    _cur_wiki5_path = _current_wiki_dir()
    _cur_wiki5 = str(_cur_wiki5_path)
    with st.expander(tf("📁 위키 저장 보관함(Vault): `%s`  (`%s`)", _cur_wiki5_path.name, _cur_wiki5), expanded=False):
        if _vaults5:
            _vault_opts5 = _vaults5 + ([] if _cur_wiki5 in _vaults5 else [_cur_wiki5])
            _vault_idx5 = _vault_opts5.index(_cur_wiki5) if _cur_wiki5 in _vault_opts5 else 0
            _vault_sel5 = st.selectbox(t("Obsidian 보관함(Vault) 선택"), _vault_opts5, index=_vault_idx5,
                                       key="wiki5_vault_sel",
                                       format_func=lambda p: f"{Path(p).name}  ({p})")
            if _vault_sel5 != _cur_wiki5:
                if st.button(t("이 보관함(Vault)로 변경 (즉시 적용)"), icon=":material/check:", key="wiki5_vault_save"):
                    set_wiki_dir(_vault_sel5)
                    st.session_state["wiki5_active_dir"] = _vault_sel5
                    st.success(f"✅ 보관함(Vault) 변경됨: {_vault_sel5}")
                    st.rerun()
        else:
            st.info(t("Obsidian 보관함(Vault) 목록을 가져올 수 없습니다. Obsidian이 설치·실행됐는지 확인하세요."))
        _custom5 = st.text_input(t("또는 직접 경로 입력"), key="wiki5_vault_custom", placeholder="/path/to/vault")
        if _custom5 and st.button(t("직접 입력 경로로 변경 (즉시 적용)"), icon=":material/check:", key="wiki5_vault_custom_save"):
            set_wiki_dir(_custom5)
            st.session_state["wiki5_active_dir"] = _custom5
            st.success(f"✅ 보관함(Vault) 변경됨: {_custom5}")
            st.rerun()

    _wiki_prov_ok5 = any(llm.has_key(p) for p in llm.PROVIDERS)
    if not _wiki_prov_ok5:
        st.warning(t("Wiki 생성 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요."))
    else:
        _settings_ai_note()

    _fws5 = DEFAULT_WS
    _wiki_stems5 = {_nfc(p.stem) for p in _cur_wiki5_path.rglob("*.md")} if _cur_wiki5_path.exists() else set()

    # ── 챕터 요약 → Wiki (큐 기반) ───────────────────────────
    _q5_stems = queue_list("tab5_ready")   # Tab4가 등록한 책 stem
    _wiki_pend5: list[dict] = []
    _wiki_done5_list: list[dict] = []
    for _stem5 in _q5_stems:
        _ch5 = chapters_dir(DEFAULT_WS, _stem5)
        _jsons5 = list_summary_files(_ch5)
        _total5 = len([f for f in _ch5.glob("??_*.txt")
                       if not f.stem.endswith(("_ko", "_wiki"))]) if _ch5.exists() else 0
        _ratio5 = tf("%d/%d챕터 요약됨", len(_jsons5), _total5)
        # 챕터 이름 목록 (NN_제목.txt → 제목)
        _ch_names5 = [_re.sub(r'^\d+_', '', f.stem) for f in sorted(_ch5.glob("??_*.txt"))
                      if not f.stem.endswith(("_ko","_wiki"))] if _ch5.exists() else []
        _has_ov5 = find_overview_file(DEFAULT_WS, _stem5) is not None
        if _stem5 in _wiki_stems5:
            _wiki_done5_list.append({"stem": _stem5, "n": len(_jsons5), "total": _total5})
        else:
            _wiki_pend5.append({
                "key": _stem5,
                "label": _stem5,
                "meta": _ratio5 + " · " + (t("전체요약 ✓") if _has_ov5 else t("전체요약 — (반영 시 자동 생성)")),
                "obj": {"ws": DEFAULT_WS, "stem": _stem5},
                "ch_names": _ch_names5,
            })

    # 챕터 요약 → Wiki
    st.markdown(tf("#### 챕터 요약 → Wiki (%d권 대기)", len(_wiki_pend5)))
    if _wiki_pend5:
        # 전체 선택 / 해제 (분할 탭 체크리스트와 동일한 조작)
        _wk5_keys = [f"wiki5_{_it5['key']}" for _it5 in _wiki_pend5]
        _wsel5c1, _wsel5c2, _wsel5c3 = st.columns([1.3, 1, 4])
        if _wsel5c1.button(t("전체 선택"), icon=":material/select_all:", key="wiki5_select_all", use_container_width=True):
            for _wk in _wk5_keys:
                st.session_state[_wk] = True
            st.rerun()
        if _wsel5c2.button(t("해제"), icon=":material/deselect:", key="wiki5_deselect_all", use_container_width=True):
            for _wk in _wk5_keys:
                st.session_state[_wk] = False
            st.rerun()
        _wsel5c3.caption(tf("총 %d권", len(_wiki_pend5)))
        # 책 단위 체크리스트 + 챕터 이름 펼치기
        _sel5: list = []
        with st.container(height=320, border=True):
            _w5h1, _w5h2, _w5h3, _w5h4 = st.columns([0.05, 0.5, 0.32, 0.13])
            _w5h2.markdown(t("**책 제목**"), unsafe_allow_html=True)
            _w5h3.markdown(f"<small style='color:#9ca3af'>{t('챕터')}</small>", unsafe_allow_html=True)
            for _it5 in _wiki_pend5:
                _k5 = f"wiki5_{_it5['key']}"
                _c5a, _c5b, _c5c, _c5d = st.columns([0.05, 0.5, 0.32, 0.13])
                _chk5 = _c5a.checkbox(" ", key=_k5, label_visibility="collapsed")
                if _chk5:
                    _sel5.append(_it5["obj"])
                _c5b.markdown(f"**{_it5['label']}**", unsafe_allow_html=True)
                _ch_preview5 = " · ".join(_it5["ch_names"][:4])
                if len(_it5["ch_names"]) > 4:
                    _ch_preview5 += f" … +{len(_it5['ch_names'])-4}개"
                _c5c.caption(_it5["meta"])
                _view_dir5 = chapters_dir(DEFAULT_WS, _it5["obj"]["stem"])
                if _c5d.button(t("보기"), icon=":material/visibility:", key=f"wiki5_view_{_it5['key']}", use_container_width=True,
                                disabled=not _view_dir5.exists()):
                    open_path(_view_dir5)
                if _it5["ch_names"]:
                    with st.expander(f"  ↳ {_ch_preview5}", expanded=False):
                        _ch5_dir = chapters_dir(DEFAULT_WS, _it5["obj"]["stem"])
                        for _cn5 in _it5["ch_names"]:
                            # NN_제목.txt → NN_제목_wiki.md(구형 json 폴백) 탐색
                            _cn5_txt = next((_ch5_dir.glob(f"??_{_cn5}.txt")), None) if _ch5_dir.exists() else None
                            _cn5_json = summary_file_for(_cn5_txt) if _cn5_txt else None
                            _has_json5 = _cn5_json is not None
                            _cj1, _cj2 = st.columns([4, 1])
                            if _has_json5:
                                _cj1.markdown(f"✅ **{_cn5}**")
                                _safe_key5 = _re.sub(r"[^a-zA-Z0-9가-힣]", "_", _cn5)[:30]
                                if _cj2.button("Wiki", icon=":material/menu_book:", key=f"ch5w_{_it5['key'][:20]}_{_safe_key5}", use_container_width=True):
                                    _bok5, _bmsg5 = build_single_chapter_wiki(DEFAULT_WS, _it5["obj"]["stem"], _cn5_json, wiki_dir=_cur_wiki5_path)
                                    (st.success if _bok5 else st.error)(
                                        f"{'✅ ' + Path(_bmsg5).name if _bok5 else '❌ ' + _bmsg5}")
                                _pv5 = load_summary_file(_cn5_json)
                                if _pv5:
                                    with st.expander(f"  📖 {_cn5[:35]}", expanded=False):
                                        if _pv5.get("summary"):
                                            st.info(_pv5["summary"])
                                        if _pv5.get("body"):
                                            st.markdown(_pv5["body"])
                            else:
                                _cj1.caption(f"⏳ {_cn5}")
        _b5c1, _b5c2 = st.columns(2)
        _rs5 = _b5c1.button(tf("시작 (%d권)", len(_sel5)), icon=":material/play_arrow:", key="wiki5_run_sel",
                              use_container_width=True, type="primary", disabled=len(_sel5)==0)
        _del5 = _b5c2.button(tf("삭제 (%d권)", len(_sel5)), icon=":material/delete:", key="wiki5_del",
                             use_container_width=True, disabled=len(_sel5)==0)
        if _del5 and _sel5:
            queue_remove("tab5_ready", [_o5["stem"] for _o5 in _sel5])
            st.rerun()
        if _rs5 and _sel5:
            _run_start("wiki5", [_o5["stem"] for _o5 in _sel5])
    else:
        st.info(t("Wiki 대기 없음 — 📝 문서요약에서 요약 완료 후 자동 등록되거나 아래에서 수동 추가하세요"))

    # 수동 추가 expander (책 단위)
    with st.expander(t("➕ 수동으로 추가 (요약 완료된 책에서 선택)")):
        _mc5a, _mc5b = st.columns([3, 2])
        _search5 = _mc5a.text_input(t("책 이름 검색"), key="wiki5_search", placeholder=t("검색어 입력…"))
        _sort5 = _mc5b.radio(t("정렬"), [t("최근 추가순"), t("이름순")], horizontal=True, key="wiki5_sort")
        _ch_root5m = cfg.CHAPTERS_DIR
        _all_books5 = list(_ch_root5m.iterdir()) if _ch_root5m.exists() else []
        _books_with_json5 = [d for d in _all_books5 if d.is_dir() and list_summary_files(d)]
        _books_with_json5 = sorted(_books_with_json5, key=lambda d: d.stat().st_mtime, reverse=True) \
                            if _sort5 == t("최근 추가순") else sorted(_books_with_json5, key=lambda d: d.name)
        _filt5 = [d for d in _books_with_json5 if _search5.lower() in d.name.lower()] if _search5 else _books_with_json5
        _mitems5 = [{"key": d.name, "label": d.name,
                     "meta": tf("%d챕터 요약", len(list_summary_files(d))), "obj": d.name}
                    for d in _filt5]
        _msel5 = _checklist(_mitems5, "wiki5m", height=200)
        _madd5c1, _madd5c2 = st.columns(2)
        if _madd5c1.button(tf("선택 항목 큐에 추가 (%d권)", len(_msel5)), icon=":material/add:", key="wiki5m_add",
                           use_container_width=True, disabled=len(_msel5)==0):
            queue_add("tab5_ready", _msel5); st.rerun()
        if _madd5c2.button(tf("삭제 (%d권)", len(_msel5)), icon=":material/delete:", key="wiki5m_del",
                           use_container_width=True, disabled=len(_msel5)==0):
            queue_remove("tab5_ready", _msel5); st.rerun()

    # 단일 TXT 기반 (챕터 분할 없는 책 — 큐 외 별도 경로)
    _single_pend5: list[dict] = []
    _t5s = cfg.TXT_DIR
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
        st.markdown(tf("#### 단일 TXT → Wiki (%d권 · 챕터 분할 없음)", len(_single_pend5)))
        st.caption(t("아직 위키로 만들지 않은 단일 TXT입니다. 위키로 만들거나, 필요 없으면 원본 TXT를 삭제할 수 있습니다."))
        _sel5s = _checklist(_single_pend5, "wiki5s", height=200)
        _s5c1, _s5c2 = st.columns(2)
        _run5s = _s5c1.button(tf("Wiki 생성 (%d권)", len(_sel5s)), icon=":material/play_arrow:", key="wiki5s_run",
                     use_container_width=True, type="primary", disabled=len(_sel5s)==0)
        _del5s = _s5c2.button(tf("삭제 (%d권)", len(_sel5s)), icon=":material/delete:", key="wiki5s_del",
                     use_container_width=True, disabled=len(_sel5s)==0)
        if _run5s and _sel5s:
            for _wo5s in _sel5s:
                _ok5s = trigger_gemini_wiki(_wo5s["txt"])
                (st.success if _ok5s else st.error)(
                    f"{'✅ 백그라운드 시작' if _ok5s else '❌ 실패'}: {_wo5s['stem']}")
            st.rerun()
        if _del5s and _sel5s:
            for _wo5s in _sel5s:
                try:
                    Path(_wo5s["txt"]).unlink(missing_ok=True)
                except Exception:
                    pass
            st.rerun()

    # Wiki 완료 목록
    st.divider()
    _wiki_files5 = sorted(_cur_wiki5_path.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) \
                   if _cur_wiki5_path.exists() else []
    st.markdown(tf("#### Wiki 완료 (%d노트)", len(_wiki_files5)))
    if _wiki_files5:
        _wv_col1, _wv_col2 = st.columns(2)
        if _wv_col1.button(t("Obsidian 보관함(Vault) 열기"), icon=":material/menu_book:", key="w5_vault", use_container_width=True):
            open_wiki_vault()
        if _wv_col2.button(t("폴더 열기"), icon=":material/folder_open:", key="w5_folder", use_container_width=True):
            open_path(_cur_wiki5_path)
        with st.container(height=300, border=True):
            for _wf5 in _wiki_files5[:100]:
                _wc1, _wc2, _wc3 = st.columns([5, 2, 1])
                _wc1.caption(f"**{_wf5.stem}**")
                _wc2.caption(datetime.fromtimestamp(_wf5.stat().st_mtime).strftime("%m-%d %H:%M"))
                if _wc3.button("", icon=":material/folder_open:", key=f"w5_open_{_wf5}", help="열기"):
                    open_path(_wf5)
    else:
        st.caption("생성된 Wiki 없음")


# ── 설정 (API 키) ─────────────────────────────────────
if _active_view == "settings":
    _lang_cur = get_lang()
    _lang_sel = st.radio("🌐 언어 / Language", ["한국어", "English"],
                         index=0 if _lang_cur == "ko" else 1,
                         horizontal=True, key="ui_lang_radio")
    _lang_new = "ko" if _lang_sel == "한국어" else "en"
    if _lang_new != _lang_cur:
        set_lang(_lang_new)
        st.rerun()
    st.divider()
    st.caption(t(
        "API 키는 이 화면에서 직접 저장한 값만 사용합니다. "
        "저장 키는 `~/.config/mybookshelf/keys.json`에만 보관되며 저장소에 올라가지 않습니다."
    ))
    with st.expander(t("저작권 및 사용 주의"), expanded=False):
        st.markdown(t(
            "**My Bookshelf** · © 2026 저작자 — 개인·비상업 연구 보조 용도. "
            "이 프로그램의 저작권은 저작자에게 있으며, 개인적·학술적 용도로 사용할 수 있으나 "
            "서면 동의 없는 재판매·상업적 배포는 허용되지 않습니다. 프로그램은 '있는 그대로' 제공되며 "
            "정확성·무결성을 보증하지 않습니다."
        ))
        st.write(t(
            "원문 문서의 저작권·번역권·요약·재배포 가능 여부는 이용자 본인이 확인해야 합니다. "
            "이 앱은 법률·출판·학술 제출 요건을 자동 판정하지 않습니다."
        ))
        st.write(t(
            "AI API 또는 CLI 구독 도구를 활성화하면 문서 일부 또는 전체가 외부 AI 서비스로 전송됩니다. "
            "개인정보, 비공개 원고, 배포 권한이 불명확한 자료는 넣지 마세요."
        ))
        st.write(t(
            "생성된 번역·요약·위키 노트의 정확성·완전성은 보장되지 않습니다. "
            "출판·제출·인용·대외 배포 전에는 반드시 원문과 결과물을 직접 대조해 검토하세요."
        ))

    # 🧠 위키 생성 모델 (공급자/모델)
    _wp, _wm = llm.wiki_provider_model()
    st.markdown(f"**🧠 위키 생성 모델** — 현재: `{_wp} · {_wm}`")
    _avail = [(p, m) for p, info in llm.PROVIDERS.items() if llm.has_key(p) for m in info["models"]]
    if _avail:
        _labels = [f"{llm.PROVIDERS[p]['label']} · {m}" for p, m in _avail]
        _curlbl = f"{llm.PROVIDERS.get(_wp, {}).get('label', _wp)} · {_wm}"
        _idx = _labels.index(_curlbl) if _curlbl in _labels else 0
        _sel = st.selectbox(t("위키 노트를 생성할 모델"), _labels, index=_idx, key="wiki_model_sel")
        _p, _m = _avail[_labels.index(_sel)]
        if (_p, _m) != (_wp, _wm) and st.button(t("이 모델로 위키 생성"), icon=":material/check:", use_container_width=True):
            llm.set_wiki_model(_p, _m); st.success(f"위키 모델 = {_p} · {_m}"); st.rerun()
    else:
        st.info(t("사용 가능한 API 키나 활성화된 CLI가 없습니다. 아래에서 API 키를 입력하거나 CLI 사용을 켜세요."))
    st.caption("번역과 별개로, 위키 노트 생성에 쓸 모델입니다. 구조화 출력은 공급자별로 자동 처리됩니다.")
    st.divider()

    # 🖥 CLI 구독 도구 — API 등록보다 앞(우선) · Claude/Codex 컴팩트 토글 (2026-07-10)
    st.markdown(t("### AI 구독 (CLI)"))
    st.caption(t("API 키 없이 구독으로 사용 — 설치·로그인 후 켜세요. AI 키 등록보다 우선합니다."))
    _cc1, _cc2 = st.columns(2)
    with _cc1:
        _claude_installed = llm.claude_cli_installed()
        _claude_enabled = bool(llm.get_pref("use_claude_cli", False))
        if _claude_installed:
            _new_enabled = st.toggle("Claude", value=_claude_enabled, key="set_use_claude_cli",
                                     help=f"설치됨: {llm.claude_cli_path()} · Claude 구독 로그인 시 켜세요")
            if _new_enabled != _claude_enabled:
                llm.set_claude_cli_enabled(_new_enabled)
                st.rerun()
        else:
            st.toggle("Claude", value=False, disabled=True, key="set_use_claude_cli", help="미설치")
            st.caption("미설치 · `npm i -g @anthropic-ai/claude-code`")
    with _cc2:
        _codex_installed = llm.codex_cli_installed()
        _codex_enabled = bool(llm.get_pref("use_codex_cli", False))
        if _codex_installed:
            _new_codex_enabled = st.toggle("Codex", value=_codex_enabled, key="set_use_codex_cli",
                                           help=f"설치됨: {llm.codex_cli_path()} · ChatGPT 로그인 시 켜세요")
            if _new_codex_enabled != _codex_enabled:
                llm.set_codex_cli_enabled(_new_codex_enabled)
                st.rerun()
        else:
            st.toggle("Codex", value=False, disabled=True, key="set_use_codex_cli", help="미설치")
            st.caption("미설치 · `npm i -g @openai/codex`")
    st.divider()

    # 🔑 API 등록 (CLI 공급자 제외)
    st.markdown("### 🔑 API 등록")
    _cli_provs = {"claude_cli", "codex_cli"}
    for _prov, _info in llm.PROVIDERS.items():
        if _prov in _cli_provs:
            continue
        _cur = llm.masked(_prov)
        _api_label = ("✅ " + t("저장됨") + " " + _cur) if _cur else t("미설정")
        with st.expander(f"{_info['label']}  —  {_api_label}",
                         expanded=not bool(_cur)):
            with st.form(f"keyform_{_prov}", clear_on_submit=True):
                _newk = st.text_input(f"{_info['label']} API 키", type="password",
                                      placeholder=_info["hint"], key=f"keyin_{_prov}")
                _c1, _c2 = st.columns(2)
                _save = _c1.form_submit_button(t("저장"), icon=":material/save:", use_container_width=True)
                _del = _c2.form_submit_button(t("삭제"), icon=":material/delete:", use_container_width=True)
                if _save:
                    if _newk.strip():
                        llm.save_key(_prov, _newk.strip())
                        st.success(t("저장됨"))
                        st.rerun()
                    else:
                        st.warning(t("키를 입력하세요."))
                if _del:
                    llm.save_key(_prov, "")
                    st.info("저장 키 삭제됨")
                    st.rerun()
            if _cur:
                st.caption("현재 앱 설정에 저장된 키를 사용합니다.")
            st.caption(f"모델: {', '.join(_info['models'])}")

    st.divider()
    st.markdown("### 📓 옵시디언(Obsidian) 보관함 설정")
    st.caption(
        f"현재: `{_current_wiki_dir()}` — 생성된 위키 노트가 여기 저장되고, "
        "Wiki 목록 탭의 [옵시디언에서 위키 보관함(Vault) 열기]도 이 폴더를 엽니다."
    )
    _default_wiki = str(cfg.BASE_DIR / "wiki")
    _wiki_cands: list[str] = []
    for _c in [_default_wiki] + list_obsidian_vaults():
        if _c and _c not in _wiki_cands:
            _wiki_cands.append(_c)
    _cur_wiki = str(_current_wiki_dir())
    _wd_sel = st.selectbox(
        "폴더 선택 — 기본값 + 옵시디언에 등록된 보관함(Vault)들",
        _wiki_cands,
        index=_wiki_cands.index(_cur_wiki) if _cur_wiki in _wiki_cands else 0,
        key="wiki_dir_sel",
    )
    _wd_custom = st.text_input("또는 폴더 경로 직접 입력 (비우면 위 선택 사용)", value="", key="wiki_dir_custom")
    _wd_target = (_wd_custom.strip() or _wd_sel).strip()
    if st.button(t("위키 보관함(Vault) 저장 (즉시 적용)"), icon=":material/save:", use_container_width=True, key="wiki_dir_save"):
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
