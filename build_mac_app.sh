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
# My Bookshelf 통합 런처
# 역할: 메뉴바 앱이 실행 중 → 브라우저 열기
#        실행 중 아님    → 메뉴바 앱 시작 (Streamlit 포함)
RESOURCES="$( cd "$( dirname "$0" )/../Resources" && pwd )"
SUPPORT="$HOME/Library/Application Support/MyBookshelf"
VENV="$SUPPORT/.venv"
LOG="$SUPPORT/app.log"
PORT=8501
MENUBAR_APP="$RESOURCES/menubar_app.py"

# ── 메뉴바 앱 이미 실행 중 → 브라우저만 열기 ──────────────
if pgrep -f "menubar_app.py" > /dev/null 2>&1; then
    open "http://localhost:$PORT"; exit 0
fi

# ── Streamlit만 실행 중(launchd 등) → 브라우저만 열기 ─────
if curl -s "http://localhost:$PORT/" >/dev/null 2>&1; then
    open "http://localhost:$PORT"; exit 0
fi

# ── 첫 실행: 패키지 설치 ────────────────────────────────────
if [ ! -d "$VENV" ]; then
    osascript -e 'display dialog "My Bookshelf를 처음 시작합니다.\n\n필요한 패키지를 설치합니다 (10~20분 소요).\n완료되면 메뉴바에 아이콘이 나타납니다.\n\n창을 닫지 마세요." buttons {"확인"} default button "확인" with title "My Bookshelf 설치"'

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
    osascript -e 'display notification "설치 완료! 메뉴바에 아이콘이 나타납니다." with title "My Bookshelf"'
fi

# ── 메뉴바 앱 시작 (Streamlit + 상태 모니터 통합) ───────────
export PATH="$VENV/bin:$PATH"
mkdir -p "$SUPPORT"

# 메뉴바 앱이 있으면 그걸로, 없으면 Streamlit 단독 실행
if [ -f "$MENUBAR_APP" ]; then
    nohup "$VENV/bin/python" "$MENUBAR_APP" >>"$LOG" 2>&1 &
    # 메뉴바 앱이 Streamlit을 내부에서 시작할 때까지 최대 25초 대기
    for i in $(seq 1 25); do
        sleep 1
        if curl -s "http://localhost:$PORT/" >/dev/null 2>&1; then
            open "http://localhost:$PORT"; exit 0
        fi
    done
else
    # 폴백: Streamlit 단독
    nohup "$VENV/bin/streamlit" run "$RESOURCES/pipeline_app.py" \
        --server.port $PORT --browser.gatherUsageStats false --server.headless true \
        >>"$LOG" 2>&1 &
    for i in $(seq 1 20); do
        sleep 1
        if curl -s "http://localhost:$PORT/" >/dev/null 2>&1; then
            open "http://localhost:$PORT"; exit 0
        fi
    done
fi
open "http://localhost:$PORT"
LAUNCHER
chmod +x "$MACOS/MyBookshelf"

# ── core/ 파일을 Resources에 복사 ──────────────────────────
cp "$SCRIPT_DIR/core/"*.py "$RESOURCES/"
cp "$SCRIPT_DIR/core/requirements.txt" "$RESOURCES/"

# ── 메뉴바 앱 + 아이콘 포함 (통합) ────────────────────────
[ -f "$HOME/.local/bin/menubar_app.py"       ] && cp "$HOME/.local/bin/menubar_app.py"       "$RESOURCES/"
[ -f "$HOME/.local/bin/menubar_icon.png"     ] && cp "$HOME/.local/bin/menubar_icon.png"     "$RESOURCES/"
[ -f "$HOME/.local/bin/menubar_icon@2x.png"  ] && cp "$HOME/.local/bin/menubar_icon@2x.png"  "$RESOURCES/"

# ── 앱 아이콘 생성 ─────────────────────────────────────────
echo "🎨 아이콘 생성 중…"
python3 - << 'PYICON'
from PIL import Image, ImageDraw
import sys, subprocess, shutil, os

SIZE = 1024

def make_icon(s):
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    r = int(s * 0.22)
    bg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    bd.rounded_rectangle([0, 0, s-1, s-1], radius=r, fill=(245, 230, 200, 255))
    for i in range(s//2, s):
        alpha = int((i - s//2) / (s//2) * 30)
        bd.line([(0, i), (s, i)], fill=(180, 140, 90, alpha))
    img = Image.alpha_composite(img, bg)
    d = ImageDraw.Draw(img)
    shelf_y = int(s * 0.76)
    shelf_h = int(s * 0.055)
    d.rounded_rectangle([int(s*0.07), shelf_y+int(s*0.015), int(s*0.93), shelf_y+shelf_h+int(s*0.015)],
                        radius=int(shelf_h*0.4), fill=(160, 120, 70, 100))
    d.rounded_rectangle([int(s*0.07), shelf_y, int(s*0.93), shelf_y+shelf_h],
                        radius=int(shelf_h*0.4), fill=(180, 130, 70, 255))
    books = [
        (0.11, 0.13, 0.52, (70, 120, 180)),
        (0.25, 0.10, 0.42, (200, 80, 70)),
        (0.36, 0.14, 0.58, (80, 160, 100)),
        (0.51, 0.11, 0.44, (180, 140, 50)),
        (0.63, 0.09, 0.38, (120, 70, 160)),
        (0.73, 0.15, 0.50, (60, 140, 170)),
    ]
    for bx, bw, bh, color in books:
        x1, x2 = int(s*bx), int(s*(bx+bw))
        y2, y1 = shelf_y, shelf_y - int(s*bh)
        br = int((x2-x1)*0.15)
        d.rounded_rectangle([x1+int(s*0.008), y1+int(s*0.010), x2+int(s*0.008), y2+int(s*0.008)],
                            radius=br, fill=(*[max(0,c-60) for c in color], 80))
        d.rounded_rectangle([x1, y1, x2, y2], radius=br, fill=(*color, 255))
        hl = max(2, int((x2-x1)*0.12))
        d.rounded_rectangle([x1, y1, x1+hl, y2], radius=br,
                            fill=(*[min(255,c+60) for c in color], 200))
        for j in range(3):
            ly = y1 + int(s*0.025) + j*int(s*0.012)
            d.line([(x1+int((x2-x1)*0.3), ly),(x2-int((x2-x1)*0.1), ly)],
                   fill=(255,255,255,80), width=max(1,int(s*0.004)))
    return img

try:
    icon = make_icon(SIZE)
    icon.save("/tmp/_mb_icon_1024.png")
    iconset = "/tmp/MyBookshelf.iconset"
    os.makedirs(iconset, exist_ok=True)
    for sz in [16,32,64,128,256,512,1024]:
        subprocess.run(["sips","-z",str(sz),str(sz),"/tmp/_mb_icon_1024.png",
                        "--out",f"{iconset}/icon_{sz}x{sz}.png","-s","format","png"],
                       capture_output=True)
    for src, dst in [(32,"16x16@2x"),(64,"32x32@2x"),(256,"128x128@2x"),
                     (512,"256x256@2x"),(1024,"512x512@2x")]:
        subprocess.run(["sips","-z",str(src),str(src),"/tmp/_mb_icon_1024.png",
                        "--out",f"{iconset}/icon_{dst}.png","-s","format","png"],
                       capture_output=True)
    subprocess.run(["iconutil","-c","icns",iconset,"-o","/tmp/MyBookshelf.icns"], check=True)
    print("  아이콘 생성 완료")
except Exception as e:
    print(f"  ⚠️ 아이콘 생성 실패 (무시): {e}", file=sys.stderr)
PYICON

if [ -f /tmp/MyBookshelf.icns ]; then
    cp /tmp/MyBookshelf.icns "$RESOURCES/MyBookshelf.icns"
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string MyBookshelf" "$CONTENTS/Info.plist" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile MyBookshelf" "$CONTENTS/Info.plist"
fi

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
