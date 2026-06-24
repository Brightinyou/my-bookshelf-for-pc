# My Bookshelf — 에이전트 인수인계 문서

> Codex CLI / Claude Code 등 AI 코딩 에이전트를 위한 완전 컨텍스트.
> 이 문서 하나로 프로젝트 전체를 파악할 수 있도록 작성됨.
> 마지막 업데이트: 2026-06-25 (v0.5.17 + 네이티브 창)

---

## 1. 프로젝트 개요

**PDF 처리 + 위키 생성 파이프라인 앱.**  
목회학박사 논문 집필 중인 신학 연구자(한국어 사용자)가 학술 PDF를 다음 순서로 처리한다:

```
PDF 업로드
  → OCR (스캔 PDF)
  → 영→한 번역
  → 옵시디언 위키 노트 자동 생성
```

- **플랫폼**: macOS(주) + Windows 11(보조)
- **UI**: Streamlit (`localhost:8501`). **배포본은 PyWebView 네이티브 창**(`core/desktop.py`)으로 표시 — 브라우저 탭 아님
- **LLM**: Gemini 2.5 Flash(위키 기본), Claude CLI / Codex CLI(구독), OpenAI/Anthropic(API 키)
- **출력**: 옵시디언 마크다운 위키 노트 (보관함(Vault), 기본 `/Volumes/SSD_990EVOPlus/llm-wiki/`)

---

## 2. 핵심 제약 — 반드시 숙지

### 2-A. SSD 동기화 필수

launchd는 **Projects 경로가 아닌 SSD 경로**를 실행한다.
코드 편집 후 반드시 동기화하지 않으면 변경이 반영되지 않는다.

```bash
# pipeline_app.py 수정 후
cp core/pipeline_app.py /Volumes/SSD_990EVOPlus/Thesis_SSD/pipeline_app.py
launchctl kickstart -k gui/$(id -u)/com.user.streamlit-pipeline

# 라이브러리 파일 수정 후
cp core/llm_providers.py  ~/.local/bin/llm_providers.py
cp core/llm_providers.py  /Volumes/SSD_990EVOPlus/Thesis_SSD/llm_providers.py
cp core/gemini_wiki.py    ~/.local/bin/gemini_wiki.py
cp core/chapter_wiki.py   ~/.local/bin/chapter_wiki.py
```

### 2-B. 한글 경로 NFC 정규화

macOS APFS는 한글 파일명을 NFD로 저장한다.
Python 소스의 한글 문자열은 NFC다.
**직접 비교하면 항상 불일치**한다.

```python
import unicodedata
def nfc(s): return unicodedata.normalize("NFC", s)

# 올바른 비교
nfc(path.stem) == nfc("목회학_서재")   # ✅
path.stem == "목회학_서재"             # ❌ 항상 False
```

### 2-C. DONE_DIR 구조 (2단계, 책 폴더 없음)

```
add_pdf_done/
  00_목회학_서재/       ← 워크스페이스 (설교 모음, 위키 대상 제외)
    1_txt/
      sermon1.txt
  01_Thesis_AI기술/     ← 워크스페이스
    1_txt/
      moralstanding_coeckelbergh.txt
```

`f.parent.name` = "1_txt", `f.parent.parent.name` = 워크스페이스명.
3단계(`<ws>/<book>/1_txt/`)로 가정하면 깨진다.

### 2-D. 설교 폴더 제외

워크스페이스 `00_목회학_서재`는 설교 325편이다.
위키 생성·배치 처리 시 **반드시 제외**한다.

```python
if nfc(ws_name) == "00_목회학_서재":
    continue
```

---

## 3. 파일 구조

```
Projects/my-bookshelf/           ← Git 저장소 (편집 여기서)
  core/
    pipeline_app.py              # Streamlit 메인 앱 (약 2,400줄)
    llm_providers.py             # LLM 추상화 레이어
    gemini_wiki.py               # 단일 패스 위키 생성
    chapter_wiki.py              # 챕터별 위키 생성 + 배치
    config.py                    # 경로 설정
    ocr_windows.py               # Windows OCR (EasyOCR + pypdfium2)
    desktop.py                   # 네이티브 창 런처 (PyWebView)
    requirements.txt
  AGENTS.md                      ← 이 파일
  DEVLOG.md                      # 개발 일지

SSD 실행 경로 (launchd가 이 경로를 실행):
  /Volumes/SSD_990EVOPlus/Thesis_SSD/
    pipeline_app.py              ← Projects에서 cp로 동기화
    llm_providers.py             ← 동일
    gemini_wiki.py               ← ~/.local/bin 에서
    chapter_wiki.py              ← ~/.local/bin 에서

~/.local/bin/                    ← 라이브러리 설치 경로
  llm_providers.py
  gemini_wiki.py
  chapter_wiki.py
```

---

## 4. 런타임 경로 전체

| 역할 | 경로 |
|------|------|
| launchd 서비스 | `com.user.streamlit-pipeline` |
| 앱 실행 파일 | `/Volumes/SSD_990EVOPlus/Thesis_SSD/pipeline_app.py` |
| 라이브러리 | `~/.local/bin/*.py` |
| PDF 원본 투입 | `/Volumes/SSD_990EVOPlus/add_pdf/raw/` |
| TXT 처리됨(processed) | `/Volumes/SSD_990EVOPlus/add_pdf/raw/processed/` |
| 완료 아카이브(done) | `/Volumes/SSD_990EVOPlus/Thesis_SSD/SynologyDrive/add_pdf_done/` |
| 위키 출력 | `/Volumes/SSD_990EVOPlus/llm-wiki/wiki/` |
| 설정 파일 | `~/.config/mybookshelf/keys.json` |
| OCR 진행률 | `/tmp/ocr_progress_<pid>.json` (사이드카) |
| 앱 로그 | `/tmp/streamlit-pipeline.log` |

---

## 5. LLM 공급자 (`llm_providers.py`)

```python
PROVIDERS = {
    "gemini":     { "label": "Google Gemini",       "env": "GEMINI_API_KEY",     "models": ["gemini-2.5-flash", "gemini-2.5-pro"] },
    "openai":     { "label": "OpenAI GPT",          "env": "OPENAI_API_KEY",     "models": ["gpt-4o", "gpt-4o-mini"] },
    "anthropic":  { "label": "Anthropic Claude",    "env": "ANTHROPIC_API_KEY",  "models": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"] },
    "claude_cli": { "label": "Claude CLI (구독)",   "env": "",                   "models": ["claude-sonnet-4-6", "claude-opus-4-8"] },
    "codex_cli":  { "label": "Codex CLI (ChatGPT)", "env": "",                   "models": ["default"] },  # ChatGPT 계정은 모델 명시 불가
}

MAX_INPUT_CHARS = {
    "gemini":    1_900_000,
    "openai":      400_000,
    "anthropic":   140_000,
    "claude_cli":  140_000,
    "codex_cli":   400_000,
}
```

**주요 함수:**

| 함수 | 역할 |
|------|------|
| `has_key(provider)` | 사용 가능 여부 (CLI 공급자는 바이너리 존재 여부) |
| `get_key(provider)` | API 키 반환 (keys.json → env 순) |
| `save_key(provider, key)` | keys.json에 저장 (Gemini는 gemini_wiki.key도 동기화) |
| `complete(provider, model, system, prompt)` | 텍스트 생성 (번역 등 범용) |
| `complete_json(provider, model, system, prompt)` | JSON 반환 보장 (위키 생성용) |
| `wiki_provider_model()` | 현재 위키 생성 설정 `(provider, model)` 반환 |
| `set_wiki_model(provider, model)` | 위키 생성 공급자/모델 변경 |
| `claude_cli_path()` / `codex_cli_path()` | CLI 바이너리 경로 탐색 |

**CLI 공급자 호출 방식:**
- `claude_cli`: `claude -p "prompt" --model ... --output-format text`
- `codex_cli`: `codex exec ...` — model이 `"default"`/`""`면 `-m` 플래그 **생략**(ChatGPT 계정 호환). 명시하면 400 오류.

---

## 6. 위키 생성 흐름

### 단일 패스 (`gemini_wiki.py`)

```python
generate(stem: str) -> dict   # {"mode": "single", "a": "/path/to/note.md"}
```

- 텍스트 전체를 한 번에 LLM에 투입
- `_max_chars()`: 현재 공급자의 `MAX_INPUT_CHARS` 반환
- 출력: `OUT_DIR/<카테고리>/<책제목>.md`

### 챕터별 (`chapter_wiki.py`)

```python
process_book(stem: str, mode: str = "auto") -> dict
# mode: "auto" | "A" | "single"
# "auto": ## 헤딩 구조 있으면 챕터별, 없으면 단일
# "A": 챕터별 노트 생성 후 overview 노트 추가
```

**배치 실행:**
```bash
python3 ~/.local/bin/chapter_wiki.py --all --mode auto        # 미생성만
python3 ~/.local/bin/chapter_wiki.py --all --regen --mode auto # 전체 재생성
python3 ~/.local/bin/chapter_wiki.py "책제목"                  # 개별
```

**백그라운드 배치 (PTY 문제 우회):**
```python
# nohup & 는 PTY 종료 시 SIGHUP을 받아 죽는다.
# os.setsid()로 새 세션 생성이 필수.
python3 - <<'PYEOF' &
import os, sys, subprocess
os.setsid()
log = open('/tmp/wiki_batch.log', 'w', buffering=1)
proc = subprocess.Popen([sys.executable, '/Users/home_mini/.local/bin/chapter_wiki.py',
                         '--all', '--mode', 'auto'], stdout=log, stderr=log)
proc.wait()
PYEOF
```

### 위키 노트 존재 여부 확인

```python
def wiki_note_exists(stem: str) -> bool:
    target = gw.make_filename(nfc(stem))
    return any(md.name == target for md in gw.OUT_DIR.rglob("*.md"))
```

---

## 7. OCR 흐름

| 플랫폼 | 엔진 | 조건 |
|--------|------|------|
| macOS | `ocrmac` (Apple Vision) | `--ocr-lang ko-KR` 필수 (누락 시 한글 전부 깨짐) |
| Windows | `EasyOCR + pypdfium2` | `ocr_windows.py` |
| 디지털 PDF | `pdftotext` 직접 추출 | OCR보다 품질 높음 |

**Docling 흐름 (스캔 PDF):**
1. Docling이 PDF 렌더링 후 페이지별 이미지 추출
2. ocrmac/EasyOCR로 OCR
3. Docling이 `##` 제목 구조 복원

**한글 텍스트레이어 직접 추출 (디지털 PDF):**
```bash
pdftotext -layout "책.pdf" "책.txt"
```
`현대사회학`, `코로나19`, `창조와타락` 등은 이 방법으로 깨끗한 텍스트 추출 확인됨.

---

## 8. Streamlit 앱 구조 (`pipeline_app.py`)

### 탭 구성 (v0.5.0~ : 6탭 + 단계별 큐)

| 탭 | 역할 | 큐 |
|----|------|-----|
| 📄 OCR/TXT | PDF 업로드(UPLOAD_TMP 대기) → 처리 버튼 → OCR/TXT 추출 | → `tab2_ready` |
| 📂 장별 분할 | TXT를 장(Chapter) 단위로 분리 (`chapter_wiki.chapter_split`) | → `tab3_ready` |
| 🌐 번역 | 챕터별 영→한 번역 | → `tab4_ready` |
| 📝 요약MD | 챕터별 위키 JSON 요약 생성 | → `tab5_ready` |
| 📚 Wiki반영 | 챕터 노트 + 전체 요약 노트를 보관함(Vault)에 생성 | — |
| ⚙️ 설정 | API 키, 위키 모델, 보관함(Vault) 폴더 | — |

- **업로드는 즉시 처리 안 함**: 파일은 `UPLOAD_TMP`에 대기 → 사용자가 처리 버튼을 눌러야 시작 (`session_state["_ocr_queued"]`로 중복 저장 차단).
- **빠른 추출(pdftotext)은 장별 분할 불가**: 텍스트 레이어엔 `##` 헤딩 구조가 없음. 장별 분할하려면 🔬 Docling 정밀변환 필요.
- **파이프라인 큐**: `DONE_DIR/My Bookshelf/.pipeline_queue.json`. 각 탭 완료 시 다음 단계 큐에 자동 등록. "큐 비우기"는 추적 목록만 지우고 **실제 파일은 안 지움**(Tab2는 큐 외에도 `1_txt/` 전체 표시).

### 위키 생성 (Tab 5)

```
챕터 _wiki.json들
  → ① 챕터별 개별 노트 (build_single_chapter_wiki)
       → 보관함(Vault)/<책이름>/<책이름 — 챕터>.md  (하위폴더)
  → ② 전체 요약 노트 (build_wiki_from_chapter_summaries)
       → 보관함(Vault)/<책이름>.md  (루트, 챕터는 [[링크]]+요약만)
  → 양방향 링크: 챕터↔책, 이전↔다음
```

- 두 함수 모두 `wiki_dir` 파라미터로 보관함 경로를 받음(Tab5 selectbox 즉시 반영).

### 단일 폴더 구조 (v0.5.0~)

6개 워크스페이스 → **`My Bookshelf/` 단일 폴더**로 통합. done 하위:

```
add_pdf_done/My Bookshelf/
  1_txt/         # OCR/추출 TXT (Gemini 입력)
  2_md/          # Docling MD (각주·표 구조)
  3_translated/  # 번역 bilingual
  chapters/<책>/ # 장별 분할 TXT + _wiki.json
  pdf/           # 원본 PDF
  .pipeline_queue.json
```

### 주요 전역 변수

```python
WIKI_DIR      # 보관함(Vault) 출력 폴더 (config에서 로드)
DONE_DIR      # 완료 아카이브 = /Volumes/.../add_pdf_done/
DEFAULT_WS    # "My Bookshelf" (단일 폴더)
UPLOAD_TMP    # 업로드 대기 폴더 (처리 버튼 누르기 전 staging)
TXT_SUB/MD_SUB/TRANS_SUB/PDF_SUB  # "1_txt"/"2_md"/"3_translated"/"pdf"
```

---

## 9. 현재 상태 (2026-06-25, v0.5.17 기준)

### 완료된 기능

- [x] PDF → OCR → 분할 → 번역 → 요약 → 위키 생성 (6탭 단계별 큐)
- [x] 멀티 LLM 공급자 (Gemini/OpenAI/Anthropic/Claude CLI/Codex CLI)
- [x] 챕터별 위키 분리 + 한국어 N장 분할 (`chapter_wiki`)
- [x] 챕터 노트 + 전체 요약 노트 함께 생성, 책 이름 하위폴더 저장 (v0.5.16~17)
- [x] 보관함(Vault) 즉시 변경 (Tab5/설정, v0.5.14~15)
- [x] **네이티브 창 (PyWebView, `desktop.py`)** — macOS 시각 검증 + 동료 더블클릭 흐름 검증 완료 (2026-06-25)
- [x] Windows 11 실기 검증 완료 (v0.3.0)
- [x] Windows 인스톨러 (InnoSetup + GitHub Actions 빌드, v0.4.4)

### 미완료 / 남은 과제

- [ ] **네이티브 창 Windows WebView2 실기 검증** (머신 없음 — Win10 구버전은 부트스트래퍼 필요)
- [ ] **네이티브 창 다듬기**: Dock 아이콘(스크립트 실행 시 파이썬 로켓 → `.app` 번들 필요), 스플래시
- [ ] **Gemini AI OCR 옵션**: 스캔 품질 낮은 PDF를 Gemini Vision으로 재처리 (제안 대기 중)
- [ ] **OpenAI/Anthropic/Codex CLI 위키 실호출 미검증**

---

## 10. 알려진 버그 / 주의사항

### claude_cli `exit 1`

배치에서 모든 책이 `RuntimeError: claude CLI exit 1: ` (빈 stderr)으로 실패.
원인: Claude CLI 세션 만료 또는 rate limit. `claude` 명령을 직접 테스트해볼 것.

### 각주 오탐 방지 필터 (v0.4.4)

`chapter_wiki.py`의 `split_by_chapter()`는 6단계 오탐 방지:
1. 짧은 제목 + 숫자만 있는 줄 차단
2. "N. Ibid.", "ibid." 패턴 차단
3. 인용 마커 없는 단독 숫자 줄 차단
4. 1~2글자 제목 차단
5. 판수("제3판") 차단
6. 문장 중간 `##` 차단

### Codex CLI ChatGPT 계정 모델 제한

ChatGPT 계정 로그인 시 모델 명시(`-m gpt-4o`) 하면 400 오류.
`_codex_cli()`는 자동으로 모델 없이 재시도하여 기본 모델(`gpt-5.5`) 사용.

### 번역 엔진 선택

번역은 `llm.complete(provider, model, system, prompt)`를 공통으로 사용.
엔진 선택 UI는 `has_key()` True인 공급자만 표시.

---

## 11. 개발 워크플로

### 변경 → 테스트 → 배포

```bash
# 1. Projects/my-bookshelf/core/ 에서 편집
# 2. 문법 검사
python3 -c "import ast; ast.parse(open('core/pipeline_app.py').read()); print('OK')"

# 3. SSD 동기화
cp core/pipeline_app.py /Volumes/SSD_990EVOPlus/Thesis_SSD/pipeline_app.py
cp core/llm_providers.py /Volumes/SSD_990EVOPlus/Thesis_SSD/llm_providers.py
cp core/llm_providers.py ~/.local/bin/llm_providers.py

# 4. 앱 재시작
launchctl kickstart -k gui/$(id -u)/com.user.streamlit-pipeline

# 5. 로그 확인
tail -f /tmp/streamlit-pipeline.log
```

### 앱 상태 확인

```bash
launchctl list com.user.streamlit-pipeline   # PID, 종료 코드
pgrep -fa streamlit                          # 실제 프로세스
tail -20 /tmp/streamlit-pipeline.err         # 오류 로그
```

### 네이티브 창 (배포본 실행 방식)

```bash
# 동료가 실제로 하는 것:
#   setup.command(맥)/setup.bat(윈) 1회 → start.command/start-app.vbs 더블클릭
# 배포본은 .venv를 직접 쓰며 desktop.py가 네이티브 창을 띄움.
.venv/bin/python core/desktop.py            # 맥 직접 실행(GUI 세션 필요)
# desktop.py: 빈 포트 탐색 → streamlit 헤드리스 기동 → 창 → 닫으면 서버 종료
# 이미 서버가 떠 있으면(8501) 그 서버를 재사용해 창만 띄움(서버 안 죽임)
```

⚠️ launchd 운영 인스턴스(:8501)는 `desktop.py`를 안 거치고 streamlit을 직접 실행한다. `desktop.py`는 **배포본(동료 zip)** 전용 — launchd 설정과 무관.

### 위키 배치 직접 실행

```bash
# 단일 테스트
python3 ~/.local/bin/chapter_wiki.py "책제목" --mode auto

# 전체 미생성 배치 (백그라운드)
python3 - <<'PYEOF' &
import os, sys, subprocess
os.setsid()
log = open('/tmp/wiki_batch.log', 'w', buffering=1)
subprocess.Popen([sys.executable, '/Users/home_mini/.local/bin/chapter_wiki.py',
                  '--all', '--mode', 'auto'], stdout=log, stderr=log).wait()
PYEOF
tail -f /tmp/wiki_batch.log
```

---

## 12. 하지 말 것

| 금지 | 이유 |
|------|------|
| SSD 동기화 없이 Projects만 편집 | launchd는 SSD 경로 실행, 변경 미반영 |
| `nfc()` 없이 한글 경로 비교 | APFS NFD 저장, 항상 불일치 |
| DONE_DIR를 3단계로 가정 | 실제 2단계 (`<ws>/1_txt/`) |
| 설교 폴더 위키 대상 포함 | 325편 설교, 처리 불필요 |
| `nohup ... &` 배치 | PTY 종료 시 SIGHUP 수신 → 죽음. `os.setsid()` 필수 |
| Codex CLI에 모델 명시 (ChatGPT 계정) | 400 오류. 자동 재시도 로직 있음 |
| pipeline_app.py에 경로 하드코딩 | `config.py`의 `BASE_DIR` 사용 |

---

## 13. 주요 의존성

```
streamlit          # UI
google-genai       # Gemini API
openai             # OpenAI API
anthropic          # Anthropic API
docling            # PDF 파싱 + OCR 조율 (별도 venv: add_pdf/.venv)
ocrmac             # macOS 전용 OCR (Apple Vision)
pdftotext          # 디지털 PDF 텍스트 추출 (Homebrew: poppler)
```

**Codex CLI 설치:**
```bash
npm install -g @openai/codex
codex login --device-auth   # ChatGPT 계정
# 또는
echo "sk-..." | codex login --with-api-key
```

**Claude CLI 설치:**
```bash
npm install -g @anthropic-ai/claude-code
claude           # 인터랙티브 로그인
```
