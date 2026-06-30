# My Bookshelf for PC

Windows PC용 My Bookshelf입니다.

PDF 또는 TXT를 넣으면 TXT 변환, 번역, 요약, Obsidian Wiki 노트 생성을 한 흐름으로 처리합니다.

## 설치

1. `Setup.exe`를 실행합니다.
2. Windows SmartScreen 경고가 뜨면 `추가 정보` -> `실행`을 선택합니다.
3. 설치가 끝나면 시작 메뉴 또는 바탕화면의 `My Bookshelf`로 실행합니다.

## 직접 실행

개발 폴더에서 직접 실행할 때는 한 번만 아래 파일을 실행합니다.

```bat
setup.bat
```

이후 앱 실행:

```bat
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

## 요구 사항

- Windows 10/11 64비트
- Python 3.10 이상
- 인터넷 연결
- 텍스트 레이어가 있는 PDF 변환용 Poppler/pdftotext
- 선택: Obsidian

스캔본처럼 텍스트 레이어가 없는 PDF는 먼저 OCR 처리해서 PDF/TXT로 변환한 뒤 사용하세요.

## 데이터 위치

기본 데이터 폴더:

```text
C:\Users\<사용자>\Documents\My Bookshelf\
```

주요 하위 폴더:

- `done/`: 처리 완료 파일
- `wiki/`: Obsidian Wiki 노트
- `failed/`: 실패 파일

설정은 사용자 프로필의 `.config/mybookshelf/config.json`에 저장됩니다.

## 포함 파일

- `Setup.exe`: 설치 파일
- `MyBookshelf.exe`: 설치 후 앱 실행 런처
- `Uninstall.exe`: 제거 파일
- `start-app.vbs`: 개발 폴더용 무창 실행
- `start.bat`: 개발 폴더용 콘솔 실행
- `stop-app.bat`: 실행 중인 앱 종료
- `setup.bat`: 개발 폴더용 의존성 설치
- `core/`: 앱 핵심 코드
