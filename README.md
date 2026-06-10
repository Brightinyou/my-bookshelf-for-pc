# 📚 My Bookshelf

PDF 책을 넣으면 **텍스트 추출(OCR) → (선택) 영→한 번역 → 옵시디언 위키 노트**까지
자동으로 만들어 주는 개인 서재 파이프라인입니다. macOS 전용(Apple Vision OCR 사용).

## 설치 (처음 한 번)

1. 이 폴더를 원하는 위치에 둡니다.
2. **`setup.command`** 를 우클릭 → **열기** (처음엔 보안 경고가 떠서 더블클릭이 안 됩니다).
   - 파이썬 3.10+가 없으면 안내 페이지가 열립니다. 설치 후 다시 실행하세요.
   - Docling(PDF 변환 엔진)이 커서 10~20분 걸릴 수 있습니다.
3. (선택) **`install-obsidian.command`** — 위키 노트를 보는 옵시디언 설치.

## 실행

**`start.command`** 더블클릭 → 브라우저에 앱이 열립니다 (http://localhost:8501).

## 첫 사용 설정

1. 앱의 **⚙️ 설정 탭**에서 사용할 AI의 API 키를 입력합니다.
   - 위키 생성: Google **Gemini** 키 권장 (책 한 권 통째 처리, 권당 약 $0.02)
   - 번역: Gemini / OpenAI GPT / Anthropic Claude 중 선택 (Claude 구독자는 CLI 로그인으로 키 없이 가능)
   - 키는 이 컴퓨터의 `~/.config/mybookshelf/` 에만 저장되며 외부로 전송되지 않습니다.
2. **업로드 탭**에 PDF를 끌어다 놓으면 파이프라인이 시작됩니다.

## 데이터 위치

모든 산출물은 기본적으로 `~/Documents/My Bookshelf/` 아래에 쌓입니다.

| 폴더 | 내용 |
|---|---|
| `done/<분류>/` | 처리 완료 PDF + TXT/MD/번역본 |
| `wiki/` | 옵시디언 위키 노트 (vault로 열기) |
| `failed/` | 실패한 파일 |

경로를 바꾸고 싶으면 `~/.config/mybookshelf/config.json` 에 원하는 키만 적으면 됩니다
(`config.py` 상단 주석에 전체 키 목록).

## 문제 해결

- **앱이 안 열림** — 터미널에서 `start.command` 를 실행해 오류 메시지를 확인하세요.
- **OCR 한글 깨짐** — Docling이 설치되어 있는지 확인 (`setup.command` 재실행).
- **위키가 안 생김** — 설정 탭에 Gemini 키가 있는지 확인.
