"""처리 로직 서비스 패키지 — Streamlit 비의존 (2026-07-03 pipeline_app.py에서 분리).

UI(st.*)를 절대 import하지 않는다. 진행률 등 UI 갱신이 필요한 함수는
progress_cb 콜백을 받는다.
"""

import sys
from pathlib import Path

# config.py·llm_providers.py·chapter_wiki.py 등 core 최상위 모듈 import 보장
_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
