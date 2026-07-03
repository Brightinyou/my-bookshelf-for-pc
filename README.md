# My Bookshelf for PC

Windows PC용 My Bookshelf입니다.

PDF 또는 TXT를 넣으면 `TXT 변환 -> 장별 분할 -> 번역 -> 문서 요약 -> Obsidian Wiki 반영` 흐름으로 작업할 수 있습니다.

## 설치

배포 파일은 `Setup.exe` 하나입니다.
**다운로드**: [최신 릴리스](https://github.com/Brightinyou/my-bookshelf-for-pc/releases/latest)에서 `Setup.exe`를 받으세요.

1. **파이썬 3.10 이상을 먼저 설치**합니다 (<https://www.python.org/downloads/>, "Add to PATH" 체크).
2. `Setup.exe`를 실행합니다. 설치 언어(한국어/English)를 고를 수 있고, 선택한 언어가 앱 화면 언어의 기본값이 됩니다.
3. Windows SmartScreen 경고가 뜨면 `추가 정보 -> 실행`을 선택합니다.
4. 설치 중 패키지 다운로드가 진행됩니다(인터넷 필요, 수 분 소요). 끝나면 시작 메뉴 또는 바탕화면의 `My Bookshelf`로 실행합니다.

PDF 텍스트 추출용 Poppler(pdftotext)는 설치 파일에 포함되어 있어 따로 설치할 필요가 없습니다.
제거는 시작 메뉴의 `Uninstall` 또는 Windows 설정 > 앱에서 합니다.
앱 화면 언어는 `⚙️ 설정 > 🌐 언어 / Language`에서 언제든 바꿀 수 있습니다.

개발 폴더에서 직접 실행할 때:

```bat
setup.bat
start-app.vbs
```

문제 확인이 필요할 때는 콘솔이 보이는 실행 파일을 사용합니다.

```bat
start.bat
```

종료:

```bat
stop-app.bat
```

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
