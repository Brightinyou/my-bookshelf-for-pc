r"""폴더 재구성 v0.9.0 자동 마이그레이션 (2026-07-07).

구조 변경: done\<ws>\{pdf,1_txt,chapters} 중첩 + Temp 업로드 폴더
  → BASE 직속 {0_업로드대기, 1_원본PDF, 2_변환TXT, 3_챕터, 실패, 로그}.

앱 기동 시 ensure_layout()이 한 번 실행:
  - 옛 위치의 파일·폴더를 새 위치로 이동 (대상이 있으면 내용물 병합)
  - 대기열(.pipeline_queue.json) 이동 + 상대 경로 재작성
  - 남은 옛 산출물(raw, 2_md, 3_translated, done 루트 잡동사니)은 _구버전보관\로
  - 완료 마커(BASE\.folders_v090)를 남겨 재실행 방지
실패해도 앱은 뜨도록 모든 단계는 개별 try/except."""

import json
import shutil
import tempfile
from pathlib import Path

import config as cfg

_MARKER = cfg.BASE_DIR / ".folders_v090"
_WS = "My Bookshelf"


def _merge_move(src: Path, dst: Path) -> int:
    """src(파일/폴더)를 dst로 이동. dst 폴더가 이미 있으면 내용물을 병합. 이동 수 반환."""
    if not src.exists():
        return 0
    moved = 0
    try:
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.move(str(src), str(dst))
                return 1
            return 0
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return 1
        for child in list(src.iterdir()):
            moved += _merge_move(child, dst / child.name)
        try:
            src.rmdir()
        except OSError:
            pass
    except Exception:
        pass
    return moved


def _rewrite_queue(old_file: Path, new_file: Path) -> None:
    """큐 파일 이동 + 챕터 상대 경로 앵커 변경 (done\\<ws>\\chapters → 3_챕터)."""
    src = old_file if old_file.exists() else (new_file if new_file.exists() else None)
    if src is None:
        return
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    def _fix(rel: str) -> str:
        r = rel.replace("\\", "/")
        for prefix in (f"{_WS}/chapters/", "chapters/"):
            if r.startswith(prefix):
                return cfg.CHAPTERS_DIR.name + "/" + r[len(prefix):]
        return rel

    for stage, items in list(data.items()):
        if isinstance(items, list):
            data[stage] = [_fix(x) if isinstance(x, str) else x for x in items]
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if old_file.exists() and old_file != new_file:
        try:
            old_file.unlink()
        except Exception:
            pass


def _pin_folder_lang() -> None:
    """폴더명 언어 고정 — 이후 UI 언어를 바꿔도 폴더가 움직이지 않게 (2026-07-08)."""
    try:
        d = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8")) if cfg.CONFIG_FILE.exists() else {}
        if d.get("folder_lang") not in ("ko", "en"):
            d["folder_lang"] = cfg.FOLDER_LANG
            cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            cfg.CONFIG_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def ensure_layout() -> bool:
    """새 폴더 트리 보장 + 1회 마이그레이션. 마이그레이션이 실제 수행되면 True."""
    for d in (cfg.UPLOAD_TMP, cfg.PDF_DIR, cfg.TXT_DIR, cfg.CHAPTERS_DIR,
              cfg.FAILED_DIR, cfg.LOG_DIR, cfg.WIKI_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    _pin_folder_lang()
    if _MARKER.exists():
        return False

    done_ws = cfg.DONE_DIR / _WS
    moved = 0
    # 1) 핵심 산출물 → 새 위치
    moved += _merge_move(done_ws / "pdf", cfg.PDF_DIR)
    moved += _merge_move(done_ws / "1_txt", cfg.TXT_DIR)
    moved += _merge_move(done_ws / "chapters", cfg.CHAPTERS_DIR)
    # 2) 업로드 대기: 옛 Temp 위치 → 0_업로드대기
    old_upload = Path(tempfile.gettempdir()) / "pipeline_uploads"
    if old_upload.resolve() != cfg.UPLOAD_TMP.resolve():
        moved += _merge_move(old_upload, cfg.UPLOAD_TMP)
    # 3) 실패·로그 (옛 영문 폴더 → 새 한글 폴더)
    for old_name, new_dir in (("failed", cfg.FAILED_DIR), ("logs", cfg.LOG_DIR)):
        old = cfg.BASE_DIR / old_name
        if old.exists() and old.resolve() != new_dir.resolve():
            moved += _merge_move(old, new_dir)
    # 4) 대기열 이동 + 경로 재작성
    _rewrite_queue(done_ws / ".pipeline_queue.json", cfg.QUEUE_FILE)
    # 5) 옛 산출물·잡동사니 → _구버전보관
    for legacy in (done_ws / "2_md", done_ws / "3_translated", cfg.RAW_DIR,
                   cfg.BASE_DIR / "wiki"):
        if legacy.exists():
            _merge_move(legacy, cfg.LEGACY_KEEP / legacy.name)
    if done_ws.exists():                      # done 루트에 남은 파일들
        for child in list(done_ws.iterdir()):
            _merge_move(child, cfg.LEGACY_KEEP / "done_기타" / child.name)
        _merge_move(cfg.DONE_DIR, cfg.LEGACY_KEEP / "done_기타")
    try:
        _MARKER.write_text(f"v0.9.0 folder migration done (folder_lang={cfg.FOLDER_LANG})\n",
                           encoding="utf-8")
    except Exception:
        pass
    try:
        from services.common import append_log
        append_log(f"폴더 재구성 v0.9.0 마이그레이션 완료 — {moved}개 항목 이동")
    except Exception:
        pass
    return True
