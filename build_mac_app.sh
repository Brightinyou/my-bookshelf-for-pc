#!/bin/bash
# build_mac_app.sh — MyBookshelf.app 빌드
# 사용: chmod +x build_mac_app.sh && ./build_mac_app.sh
# 결과: dist/MyBookshelf.app  (우클릭→열기로 실행, 공증 불필요)
#       dist/MyBookshelf.dmg  (선택, 전달용)

set -e
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DIST="$SCRIPT_DIR/dist"
APP="$DIST/MyBookshelf.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

echo "📦 MyBookshelf.app 빌드 시작…"
rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES"

# ── Info.plist ─────────────────────────────────────────────
cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key>    <string>MyBookshelf</string>
  <key>CFBundleIdentifier</key>   <string>com.mybookshelf.app</string>
  <key>CFBundleName</key>         <string>My Bookshelf</string>
  <key>CFBundleVersion</key>      <string>1.0</string>
  <key>CFBundleShortVersionString</key> <string>1.0</string>
  <key>CFBundlePackageType</key>  <string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# ── 런처 스크립트 ───────────────────────────────────────────
cat > "$MACOS/MyBookshelf" <<'LAUNCHER'
#!/bin/bash
RESOURCES="$( cd "$( dirname "$0" )/../Resources" && pwd )"
SUPPORT="$HOME/Library/Application Support/MyBookshelf"
VENV="$SUPPORT/.venv"
LOG="$SUPPORT/app.log"
PORT=8501

# 이미 실행 중이면 브라우저만 열기
if curl -s "http://localhost:$PORT/" >/dev/null 2>&1; then
    open "http://localhost:$PORT"; exit 0
fi

# 첫 실행: 패키지 설치
if [ ! -d "$VENV" ]; then
    osascript <<OSASCRIPT
    display dialog "My Bookshelf를 처음 시작합니다.\n\n필요한 패키지를 설치합니다 (10~20분 소요).\n설치가 완료되면 브라우저가 자동으로 열립니다.\n\n창을 닫지 마세요." buttons {"확인"} default button "확인" with title "My Bookshelf 설치"
OSASCRIPT

    PY=""
    for cand in python3.13 python3.12 python3.11 python3.10 \
                /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            ver=$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo 0.0)
            major=${ver%%.*}; minor=${ver##*.}
            if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
                PY=$(command -v "$cand"); break
            fi
        fi
    done

    if [ -z "$PY" ]; then
        osascript -e 'display alert "파이썬 3.10+ 필요" message "python.org/downloads 에서 설치 후 앱을 다시 실행하세요." as critical'
        open "https://www.python.org/downloads/"; exit 1
    fi

    mkdir -p "$SUPPORT"
    "$PY" -m venv "$VENV" >>"$LOG" 2>&1
    "$VENV/bin/python" -m pip install --upgrade pip -q >>"$LOG" 2>&1
    "$VENV/bin/python" -m pip install -r "$RESOURCES/requirements.txt" -q >>"$LOG" 2>&1

    if [ ! -x "$VENV/bin/streamlit" ]; then
        osascript -e "display alert \"설치 실패\" message \"패키지 설치에 실패했습니다.\n로그: $LOG\" as critical"
        open "$SUPPORT"; exit 1
    fi
    osascript -e 'display notification "설치 완료! 브라우저가 곧 열립니다." with title "My Bookshelf"'
fi

# Streamlit 실행
export PATH="$VENV/bin:$PATH"
mkdir -p "$SUPPORT"
"$VENV/bin/streamlit" run "$RESOURCES/pipeline_app.py" \
    --server.port $PORT --browser.gatherUsageStats false --server.headless true \
    > "$LOG" 2>&1 &

# 서버 기동 대기 후 브라우저 열기 (최대 20초)
for i in $(seq 1 20); do
    sleep 1
    if curl -s "http://localhost:$PORT/" >/dev/null 2>&1; then
        open "http://localhost:$PORT"; exit 0
    fi
done
open "http://localhost:$PORT"
LAUNCHER
chmod +x "$MACOS/MyBookshelf"

# ── core/ 파일을 Resources에 복사 ──────────────────────────
cp "$SCRIPT_DIR/core/"*.py "$RESOURCES/"
cp "$SCRIPT_DIR/core/requirements.txt" "$RESOURCES/"

echo "✅ 완료: $APP"
echo

# ── DMG 생성 (선택) ────────────────────────────────────────
read -n 1 -r -p "DMG 파일도 만들까요? (y/n) " yn; echo
if [[ "$yn" =~ [Yy] ]]; then
    DMG="$DIST/MyBookshelf.dmg"
    rm -f "$DMG"
    hdiutil create \
        -volname "My Bookshelf" \
        -srcfolder "$DIST" \
        -ov -format UDZO \
        "$DMG"
    echo "✅ DMG: $DMG"
fi

echo
echo "배포 방법:"
echo "  1. dist/MyBookshelf.app 을 동료에게 전달"
echo "  2. 동료: 우클릭 → 열기 (첫 실행 시 Gatekeeper 경고 무시)"
echo "  3. 첫 실행: 패키지 자동 설치 (10~20분)"
echo "  4. 이후: 더블클릭만 하면 브라우저가 열림"
