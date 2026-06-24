#!/bin/bash
# My Bookshelf 실행 스크립트 — 더블클릭하면 앱 창이 열립니다.
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

echo "📚 My Bookshelf 를 시작합니다 — 잠시 후 앱 창이 열립니다."
echo "   앱 창을 닫으면 자동으로 종료됩니다."
# 네이티브 창(PyWebView)으로 실행. 모듈 없으면 브라우저 모드로 폴백.
if python -c "import webview" 2>/dev/null; then
    # 앱 창을 터미널과 분리해 띄우고, 이 터미널 창은 닫는다.
    nohup python core/desktop.py >/dev/null 2>&1 &
    disown
    sleep 1
    osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1
    exit 0
else
    echo "⚠️ 네이티브 창 모듈(pywebview)이 없어 브라우저로 엽니다."
    echo "   setup.command 를 다시 실행하면 네이티브 창을 쓸 수 있습니다."
    exec python -m streamlit run core/pipeline_app.py \
        --server.port 8501 \
        --browser.gatherUsageStats false
fi
