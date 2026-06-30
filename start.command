#!/bin/bash
# My Bookshelf 실행 스크립트 — 더블클릭하면 설치된 앱을 백그라운드로 엽니다.
# Terminal 창은 잠깐 뜰 수 있으나 즉시 닫히도록 시도합니다.
set -e
SCRIPT_DIR="$( cd "$( dirname "$0" )" && pwd )"
ROOT_DIR="$SCRIPT_DIR"
cd "$ROOT_DIR"

APP_CANDIDATES=(
    "/Applications/MyBookshelf.app"
    "$ROOT_DIR/dist/mac/MyBookshelf.app"
    "$ROOT_DIR/dist/MyBookshelf.app"
)

APP_PATH=""
for p in "${APP_CANDIDATES[@]}"; do
    if [ -d "$p" ]; then
        APP_PATH="$p"
        break
    fi
done

if [ -z "$APP_PATH" ]; then
    osascript -e 'display alert "My Bookshelf 앱을 찾을 수 없습니다." message "먼저 빌드/설치를 완료한 뒤 다시 실행하세요." as critical' >/dev/null 2>&1
    exit 1
fi

APP_BIN="$APP_PATH/Contents/MacOS/MyBookshelf"
if [ ! -x "$APP_BIN" ]; then
    osascript -e 'display alert "My Bookshelf 실행 파일을 찾을 수 없습니다." message "앱 번들을 다시 설치하거나 빌드하세요." as critical' >/dev/null 2>&1
    exit 1
fi

nohup "$APP_BIN" >/dev/null 2>&1 &
osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &
exit 0
