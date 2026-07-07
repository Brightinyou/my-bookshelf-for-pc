"""파이프라인 큐 — 각 탭이 완료한 항목을 다음 탭 큐에 등록하는 단방향 파이프라인.

큐 파일: done/My Bookshelf/.pipeline_queue.json
단계: tab2_ready(분할), tab3_ready(번역), tab4_ready(요약), tab5_ready(Wiki)
"""

import json

import config as cfg

from services.common import DEFAULT_WS

_QUEUE_FILE = cfg.QUEUE_FILE   # v0.9.0: BASE 루트
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
