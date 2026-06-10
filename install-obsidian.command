#!/bin/bash
# 옵시디언(Obsidian) 설치 도우미 — 위키 노트를 보는 앱입니다.
cd "$(dirname "$0")"

if [ -d "/Applications/Obsidian.app" ]; then
    echo "✅ 옵시디언이 이미 설치되어 있습니다."
elif command -v brew >/dev/null 2>&1; then
    echo "📦 Homebrew로 옵시디언을 설치합니다…"
    brew install --cask obsidian
    echo "✅ 설치 완료."
else
    echo "🌐 옵시디언 공식 다운로드 페이지를 엽니다 — DMG를 받아 설치해 주세요."
    open "https://obsidian.md/download"
fi

echo
echo "ℹ️  설치 후 옵시디언에서 'Open folder as vault'로"
echo "   ~/Documents/My Bookshelf/wiki 폴더를 열면 생성된 노트가 보입니다."
read -n 1 -s -r -p "아무 키나 누르면 창이 닫힙니다…"
