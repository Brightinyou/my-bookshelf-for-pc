"""done 폴더 구조 헬퍼 — 산출물 탐색, bilingual 파싱·저장, 처리됨 캐시.

폴더 구조:
  done/<ws>/<file>.pdf      ← PDF는 워크스페이스 루트
  done/<ws>/_txt/<file>.txt ← MD 성공 시 TXT는 _txt/
  done/<ws>/_md/<file>.md   ← MD는 _md/ (분할본도 동일)
  MD 생성 실패 시 TXT는 루트에 남아 미완료 신호로 사용
"""

import re as _re
import shutil
import time
from pathlib import Path

import config as cfg

from services.common import (
    MD_SUB, TRANS_SUB, TXT_SUB, _nfc, append_log,
)

DONE_DIR           = cfg.DONE_DIR
OLD_TRANSLATED_DIR = cfg.OLD_TRANSLATED_DIR


def txt_dir(base: Path, ws_name: str) -> Path:
    return base / ws_name / TXT_SUB

def md_dir(base: Path, ws_name: str) -> Path:
    return base / ws_name / MD_SUB

def translated_dir(base: Path, ws_name: str) -> Path:
    """bilingual.txt를 두는 폴더. done/<ws>/_translated/. (2026-05-18 통합)"""
    return base / ws_name / TRANS_SUB


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
    """_txt/ 우선 → 1_txt/완료/(분할 후 보관, 2026-07-07) → 워크스페이스 루트."""
    p1 = txt_dir(base, ws_name) / f"{stem}.txt"
    if p1.exists(): return p1
    p_arch = txt_dir(base, ws_name) / "완료" / f"{stem}.txt"
    if p_arch.exists(): return p_arch
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
