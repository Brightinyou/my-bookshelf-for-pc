#!/bin/bash
# My Bookshelf 설치 스크립트 — 더블클릭(또는 우클릭→열기)으로 실행하세요.
# 하는 일: 파이썬 확인 → 전용 가상환경(.venv) 생성 → 필요 패키지 설치.
# 인터넷 연결 필요. Docling(PDF 변환 엔진)이 커서 처음 설치는 10~20분 걸릴 수 있습니다.
set -e
cd "$(dirname "$0")"

echo "📚 My Bookshelf 설치를 시작합니다."
echo

# ── 1. 파이썬 3.10+ 찾기 ──────────────────────────────────
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo 0.0)
        major=${ver%%.*}; minor=${ver##*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            PY=$(command -v "$cand")
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "❌ 파이썬 3.10 이상이 필요합니다."
    echo "   https://www.python.org/downloads/ 에서 최신 파이썬을 설치한 뒤"
    echo "   이 스크립트를 다시 실행해 주세요."
    open "https://www.python.org/downloads/"
    exit 1
fi
echo "✅ 파이썬: $PY ($("$PY" --version 2>&1))"

# ── 2. 가상환경 + 패키지 설치 ─────────────────────────────
if [ ! -d .venv ]; then
    echo "📦 가상환경(.venv) 생성 중…"
    "$PY" -m venv .venv
fi
echo "📦 패키지 설치 중 — Docling이 커서 10~20분 걸릴 수 있습니다. 창을 닫지 마세요."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# ── 3. pdftotext(폴백 변환기) 안내 — 없어도 Docling만으로 동작 ──
if ! command -v pdftotext >/dev/null 2>&1; then
    echo "ℹ️  (선택) pdftotext 폴백이 없습니다. Homebrew가 있다면: brew install poppler"
fi

echo
echo "🎉 설치 완료! 이제 start.command 를 더블클릭하면 앱이 열립니다."
echo "   (옵시디언이 없다면 install-obsidian.command 도 실행하세요.)"
read -n 1 -s -r -p "아무 키나 누르면 창이 닫힙니다…"
