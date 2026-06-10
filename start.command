#!/bin/bash
# My Bookshelf 실행 스크립트 — 더블클릭하면 브라우저에 앱이 열립니다.
# (먼저 setup.command 로 설치를 한 번 마쳐야 합니다.)
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "❌ 설치가 안 되어 있습니다. setup.command 를 먼저 실행해 주세요."
    read -n 1 -s -r -p "아무 키나 누르면 창이 닫힙니다…"
    exit 1
fi

# venv 활성화 — PATH에 .venv/bin이 들어가 docling 자동 탐지가 작동한다.
source .venv/bin/activate

echo "📚 My Bookshelf 를 시작합니다 — 잠시 후 브라우저가 열립니다."
echo "   끝낼 때는 이 창에서 Ctrl+C 를 누르거나 창을 닫으세요."
exec python -m streamlit run pipeline_app.py \
    --server.port 8501 \
    --browser.gatherUsageStats false
