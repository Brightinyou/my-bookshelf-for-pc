# 📚 My Bookshelf

PDF 책을 넣으면 **TXT 변환 → (선택) 영→한 번역 → 옵시디언 위키 노트**까지
자동으로 만들어 주는 개인 서재 파이프라인입니다. **맥·윈도우 모두 지원합니다.**

## 시스템 요구사항

| | 맥 | 윈도우 |
|---|---|---|
| 기기 | **Apple Silicon(M1 이후)** — 인텔 맥 불가(PyTorch 미지원) | Windows 10/11 64비트 |
| OS | macOS 13(Ventura) 이상 | — |
| 공통 | 파이썬 3.10+ (없으면 설치 안내가 자동으로 열림), 디스크 여유 약 1GB, 설치 시 인터넷 | |
| PDF 변환 | 텍스트 레이어가 있는 PDF 지원 (`pdftotext`) | 텍스트 레이어가 있는 PDF 지원 (`pdftotext`) |

※ 스캔본처럼 텍스트 레이어가 없는 PDF는 먼저 OCR 처리된 PDF/TXT로 변환한 뒤 사용하세요.

## 설치 (처음 한 번)

1. 이 폴더를 원하는 위치에 둡니다.
2. 설치 스크립트 실행:
   - **맥**: `platform/mac/setup.command` 를 우클릭 → **열기** (처음엔 보안 경고가 떠서 더블클릭이 안 됩니다).
   - **윈도우**: `platform/windows/setup.bat` 더블클릭. "Windows의 PC 보호" 경고가 뜨면 **추가 정보 → 실행**.
     파이썬 설치 시 첫 화면에서 **"Add python.exe to PATH" 체크 필수**.
   - 파이썬 3.10+가 없으면 안내 페이지가 열립니다. 설치 후 다시 실행하세요.
   - 처음 설치는 몇 분 걸릴 수 있습니다.
3. (선택) 옵시디언 설치 — 위키 노트 열람용: `platform/mac/install-obsidian.command` / `platform/windows/install-obsidian.bat`.

## 실행

- **맥**: `platform/mac/start.command` 더블클릭
- **윈도우**: `platform/windows/start-app.vbs` 더블클릭 — 검은 창 없이 실행됩니다. 끌 때는 `platform/windows/stop-app.bat`.
  (오류 메시지를 봐야 할 때만 `platform/windows/start.bat`으로 실행)

→ 브라우저에 앱이 열립니다 (http://localhost:8501).

## 첫 사용 설정

1. 앱의 **⚙️ 설정 탭**에서 사용할 AI의 API 키를 입력합니다.
   - 위키 생성: Google **Gemini** 키 권장 (책 한 권 통째 처리, 권당 약 $0.02)
   - 번역: Gemini / OpenAI GPT / Anthropic Claude 중 선택 (Claude 구독자는 CLI 로그인으로 키 없이 가능)
   - 키는 이 컴퓨터의 홈 폴더 `.config/mybookshelf/` 에만 저장되며 외부로 전송되지 않습니다.
2. **1·TXT변환 탭**에 PDF/TXT를 업로드하면 파이프라인이 시작됩니다.

## 데이터 위치

모든 산출물은 기본적으로 **문서(Documents) 폴더의 `My Bookshelf/`** 아래에 쌓입니다
(맥 `~/Documents/My Bookshelf/`, 윈도우 `C:\Users\이름\Documents\My Bookshelf\`).

| 폴더 | 내용 |
|---|---|
| `done/<분류>/` | 처리 완료 PDF + TXT/MD/번역본 |
| `wiki/` | 옵시디언 위키 노트 (vault로 열기) |
| `failed/` | 실패한 파일 |

경로를 바꾸고 싶으면 홈 폴더의 `.config/mybookshelf/config.json` 에 원하는 키만 적으면 됩니다
(`config.py` 상단 주석에 전체 키 목록).

## 개발 구조

- `core/`: 맥·윈도우가 공유하는 앱 핵심 코드
- `platform/mac/`: macOS 실행·설치·앱 번들 빌드 파일
- `platform/windows/`: Windows 실행·설치·Inno Setup 파일과 `.exe` 산출물
- `dist/mac/`, `dist/windows/`: 플랫폼별 배포 산출물

## 문제 해결

- **앱이 안 열림** — 터미널(맥) 또는 명령 프롬프트(윈도우)에서 start 스크립트를 실행해 오류 메시지를 확인하세요.
- **PDF 변환 실패** — 텍스트 레이어가 있는 PDF인지 확인하세요. 스캔본은 먼저 OCR 처리된 PDF/TXT로 변환해야 합니다.
- **위키가 안 생김** — 설정 탭에 Gemini 키가 있는지 확인.
