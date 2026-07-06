# My Bookshelf for PC

Windows PC용 My Bookshelf입니다.

PDF 또는 TXT를 넣으면 `TXT 변환 -> 장별 분할 -> 번역 -> 문서 요약 -> Obsidian Wiki 반영` 흐름으로 작업할 수 있습니다.

## 설치 (처음 사용자용 상세 안내)

배포 파일은 `Setup.exe` 하나입니다.
**다운로드**: [최신 릴리스](https://github.com/Brightinyou/my-bookshelf-for-pc/releases/latest) 페이지에서 `Setup.exe`를 받으세요.

### 0단계 — 사전 준비물 확인

| 준비물 | 설명 |
|--------|------|
| Windows 10 / 11 (64비트) | 대부분의 최신 PC에 해당 |
| 인터넷 연결 | 설치 중 패키지 다운로드와 AI 호출에 필요 |
| **Python 3.10 이상** | 없으면 `Setup.exe`가 **Python 3.14.6 자동 설치**를 제안합니다. 실패할 때만 아래 1단계 수동 설치를 따르세요. |
| (선택) Obsidian | 생성된 위키 노트를 열람할 때 사용 |

PDF 텍스트 추출 도구(Poppler)는 `Setup.exe`에 포함되어 있어 **따로 설치할 필요가 없습니다.**

### 1단계 — 파이썬 준비 (자동 설치 또는 수동 설치)

이미 파이썬 3.10 이상이 설치돼 있다면 건너뜁니다.
파이썬이 없으면 `Setup.exe`가 **Python 3.14.6 자동 설치**를 먼저 제안합니다.
자동 설치가 막히거나 실패할 때만 아래 수동 설치를 진행하세요.

1. <https://www.python.org/downloads/> 에 접속해 노란색 **"Download Python 3.x.x"** 버튼을 누릅니다.
2. 받은 파일을 실행하면 설치 화면이 뜹니다. **첫 화면 맨 아래의 "Add python.exe to PATH"를 반드시 체크**한 뒤 "Install Now"를 누릅니다. (이 체크를 빼먹는 것이 가장 흔한 실패 원인입니다)
3. 설치 확인: 시작 메뉴에서 `cmd`를 실행하고 `python --version`을 입력합니다. `Python 3.1x.x`처럼 나오면 성공입니다.

### 2단계 — My Bookshelf 설치

1. 릴리스 페이지에서 받은 `Setup.exe`를 실행합니다.
2. Windows SmartScreen 경고가 뜨면 `추가 정보 → 실행`을 선택합니다. (서명되지 않은 개인 배포 프로그램이라 뜨는 정상적인 경고입니다)
3. **설치 언어(한국어/English)를 선택**합니다. 여기서 고른 언어가 앱 화면 언어의 기본값이 됩니다.
4. 설치가 진행되는 동안 파이썬 패키지 다운로드로 **수 분이 걸립니다.** 창을 닫지 말고 기다려 주세요.
5. 설치가 끝나면 앱이 자동 실행됩니다. 이후에는 바탕화면 또는 시작 메뉴의 **My Bookshelf** 아이콘으로 실행합니다.

### 3단계 — 첫 실행 설정

1. 앱의 `⚙️ 설정`에서 사용할 **AI API 키를 입력**하거나 CLI 구독 도구(Claude/Codex)를 활성화합니다.
2. Obsidian Wiki 보관함(Vault) 경로를 확인합니다.
3. 화면 언어는 `⚙️ 설정 → 🌐 언어 / Language`에서 언제든 바꿀 수 있습니다.

### 구독은 있는데 CLI가 없다면 (API 키 없이 쓰기)

Claude(Pro/Max)나 ChatGPT(Plus/Pro)를 구독 중이라면 API 키 없이 CLI 도구로 번역·요약을 쓸 수 있습니다. CLI를 설치하고 로그인한 뒤, 앱의 `⚙️ 설정`에서 해당 CLI를 **활성**으로 켜면 엔진 목록에 나타납니다.

**Claude CLI (Claude 구독자)**

1. PowerShell을 열고 다음 한 줄을 실행합니다 (Node.js 불필요):
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
   위 방법이 안 되면 Node.js 설치 후 `npm install -g @anthropic-ai/claude-code`
2. 새 터미널에서 `claude`를 실행하면 브라우저가 열립니다 — 구독 계정으로 로그인합니다.
3. 확인: `claude --version`이 버전을 출력하면 성공.

**Codex CLI (ChatGPT 구독자)**

1. Node.js LTS를 먼저 설치합니다 (<https://nodejs.org>, 기본 옵션으로 설치).
2. 터미널에서: `npm install -g @openai/codex`
3. `codex login`을 실행해 브라우저에서 ChatGPT 계정으로 로그인합니다.
4. 확인: `codex --version`이 버전을 출력하면 성공.

설치·로그인 후 앱을 재시작(또는 `stop-app.bat` 후 재실행)하면 설정 탭에서 인식됩니다.
로그인은 브라우저 인증 방식이라 자동화할 수 없습니다 — 본인 계정으로 한 번만 하면 됩니다.

### 문제가 생기면

- **"Python 3.10 or newer is required" 안내가 뜸** → 자동 설치를 취소했거나 실패한 경우입니다. 1단계 수동 설치 후 Setup.exe를 다시 실행하세요.
- **설치가 실패함** → 설치 폴더(`%localappdata%\My Bookshelf`)의 `install.log`를 열어 마지막 줄을 확인하세요. 대부분 인터넷 연결 또는 파이썬 PATH 문제입니다.
- **설치는 끝났는데 앱이 안 열림** → 설치 폴더(`%localappdata%\My Bookshelf`)의 `launch-error.log`를 확인하세요. 실행 시작 단계의 오류가 여기에 기록됩니다.
- **업데이트했는데 이전 버전이 보임** → 설치 폴더의 `stop-app.bat`을 실행해 기존 서버를 끈 뒤 아이콘으로 다시 실행하세요.
- **제거** → 시작 메뉴의 `Uninstall` 또는 Windows 설정 > 앱.

### 개발 폴더에서 직접 실행 (개발자용)

```bat
setup.bat        :: 의존성 설치 (처음 한 번)
start.bat        :: 실행 (레포 코드로 실행됨)
stop-app.bat     :: 종료
```

바탕화면 아이콘(MyBookshelf.exe)은 항상 **설치본**을, 레포 폴더의 `start.bat`은 **레포 코드**를 실행합니다. 둘을 오갈 때는 `stop-app.bat`을 먼저 실행하세요.

## 기본 사용 순서

1. `⚙️ 설정`
   - 사용할 API 키를 저장하거나 CLI 구독 도구를 활성화합니다.
   - 사용할 Obsidian Wiki 보관함(Vault) 경로를 확인합니다.
2. `📄 TXT변환 앱`
   - PDF 또는 TXT를 올려 텍스트를 준비합니다. 원본 PDF는 pdf/ 폴더에 보관됩니다.
3. `📂 장분할 앱`
   - 책/문서를 장 단위 파일로 나눕니다.
   - 짧은 문서·장 구조가 없는 문서는 단일장으로 저장할 수 있습니다.
4. `🌐 영문번역 앱`
   - 필요한 경우 챕터별 번역을 진행합니다. 중단돼도 다시 실행하면 이어서 번역합니다.
5. `📝 문서요약 앱`
   - 챕터별 요약 노트(`_wiki.md`)를 생성합니다. 위키반영 전에 열어서 고칠 수 있습니다.
6. `📖 위키반영 앱`
   - 요약 결과를 Obsidian Wiki 노트로 저장합니다.

## 요구 사항

- Windows 10/11 64비트
- Python 3.10 이상
- 인터넷 연결 (설치 시 패키지 다운로드, AI API 호출)
- 선택: Obsidian (위키 노트 열람용)

스캔본처럼 텍스트 레이어가 없는 PDF는 먼저 OCR 처리해서 PDF/TXT로 변환한 뒤 사용하세요.

## 데이터 위치

기본 데이터 폴더:

```text
C:\Users\<사용자>\Documents\My Bookshelf\
```

주요 하위 폴더:

- `done/`: 처리 중간 산출물과 완료 파일
- `wiki/`: Obsidian Wiki 노트
- `failed/`: 실패 파일
- `logs/`: 로그와 결과 기록

설정은 사용자 프로필의 `.config/mybookshelf/config.json`에 저장됩니다.

## 첫 실행 안내

- 이 앱은 사용자가 제공한 문서를 정리하고 개인 작업 흐름에 맞게 재구성하는 도구입니다.
- API 또는 CLI 도구를 활성화하면 문서 일부 또는 전체가 외부 서비스로 전송될 수 있습니다.
- 민감정보, 비공개 원고, 배포 권한이 불명확한 자료는 넣지 않는 것을 권장합니다.

## 저작권 및 면책

- 원문 문서의 저작권, 번역권, 요약/재배포 가능 여부는 사용자 책임으로 확인해야 합니다.
- 이 앱은 법률, 출판, 학술 제출 요건을 자동 판정하지 않습니다.
- 생성된 번역, 요약, 위키 노트의 정확성이나 완전성은 보장되지 않습니다.
- 출판, 제출, 인용, 대외 배포 전에는 반드시 원문과 결과물을 직접 검토하세요.

## 폴더 구성 (개발자용)

- `core/`: 앱 핵심 코드 (`services/` = 처리 로직 패키지)
- `MyBookshelf.exe`: 앱 실행 런처 — 인스톨러 빌드 재료 (Setup.exe에 번들됨)
- `vendor/poppler/`: 번들용 pdftotext — 없으면 `dev/installer/fetch-poppler.ps1` 실행
- `start-app.vbs` / `start.bat`: 개발 폴더용 실행 (무창/콘솔)
- `stop-app.bat`: 실행 중인 앱 종료
- `setup.bat`: 개발 폴더용 의존성 설치
- 빌드 산출물(`Setup.exe`, `Uninstall.exe`)은 `dist\windows\`에 생성되며 저장소에 포함하지 않습니다.
