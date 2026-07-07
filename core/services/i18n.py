"""UI 언어 (2026-07-03) — 기본 한국어(ko), 영어(en) 지원.

언어 결정 우선순위:
  1. 환경변수 MYBOOKSHELF_LANG (테스트/강제용)
  2. config.json의 "lang" (앱 설정 화면에서 저장)
  3. {app}\\app_lang.txt (인스톨러가 설치 언어 선택을 기록)
  4. "ko"

번역 방식: 한국어 원문 문자열 자체를 키로 쓰는 사전(_EN).
사전에 없는 문자열은 원문 그대로 반환하므로, 누락돼도 깨지지 않고
한국어로 표시될 뿐이다. 서식 문자열은 tf("... %d개", n)를 쓴다.
"""

import json
import os
from pathlib import Path

import config as cfg

_APP_LANG_FILE = Path(cfg.__file__).resolve().parent.parent / "app_lang.txt"

_lang_cache: str | None = None


def get_lang() -> str:
    global _lang_cache
    if _lang_cache in ("ko", "en"):
        return _lang_cache
    lang = (os.environ.get("MYBOOKSHELF_LANG") or "").strip().lower()
    if lang not in ("ko", "en"):
        try:
            d = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8")) if cfg.CONFIG_FILE.exists() else {}
            lang = str(d.get("lang", "")).strip().lower()
        except Exception:
            lang = ""
    if lang not in ("ko", "en") and _APP_LANG_FILE.exists():
        try:
            lang = _APP_LANG_FILE.read_text(encoding="utf-8", errors="ignore").strip().lower()
        except Exception:
            lang = ""
    _lang_cache = lang if lang in ("ko", "en") else "ko"
    return _lang_cache


def set_lang(lang: str) -> None:
    """config.json에 언어 저장 — 인스톨러 기본값(app_lang.txt)보다 우선."""
    global _lang_cache
    if lang not in ("ko", "en"):
        return
    f = cfg.CONFIG_FILE
    try:
        d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        d = {}
    d["lang"] = lang
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    _lang_cache = lang


def t(s: str) -> str:
    """한국어 원문 → 현재 언어. 사전에 없으면 원문 그대로."""
    if get_lang() == "ko":
        return s
    return _EN.get(s, s)


def tf(s: str, *args) -> str:
    """서식 문자열 번역 후 % 포매팅. 예: tf("총 %d개", n)"""
    return t(s) % args


_EN: dict[str, str] = {
    # ── 헤더 / 상태 배너 ──────────────────────────────────
    "PDF → TXT변환 → 장별 분할 → 영문번역 → 요약생성 → Obsidian Wiki":
        "PDF → Text → Chapter split → Translation → Summaries → Obsidian Wiki",
    "API 키": "API keys",
    "CLI 구독": "CLI tools",
    "위키 생성기": "Wiki builder",
    "Wiki 완성": "Wiki notes",
    "%d개": "%d",
    "❌ 없음": "❌ none",
    "없음": "none",
    "🔄 생성 중": "🔄 running",
    "대기": "idle",
    "⚠️ 사용 가능한 AI가 없습니다 — ⚙️ 설정 탭에서 API 키를 입력하거나 CLI 구독 도구를 활성화하세요.":
        "⚠️ No AI available — enter an API key or enable a CLI tool in ⚙️ Settings.",

    # ── 메뉴 ─────────────────────────────────────────────
    "#### 작업 메뉴": "#### Apps",
    "처음 사용 전 확인: 이 앱은 사용자가 제공한 PDF/TXT를 정리, 번역, 요약, 위키 노트로 재구성하는 개인 작업 도구입니다. "
    "원문 저작권과 이용허락은 사용자 책임으로 확인해야 하며, 외부 AI/CLI로 전송되는 텍스트에는 민감정보나 배포 권한이 불분명한 내용을 넣지 마세요.":
        "Before first use: this is a personal tool that organizes, translates, summarizes and turns your PDF/TXT into wiki notes. "
        "You are responsible for copyright and usage rights of the source material; do not submit sensitive or restricted content, as text is sent to external AI/CLI services.",
    "📄 TXT변환 앱": "📄 Text Converter",
    "PDF/TXT를 텍스트로 변환 · 업로드 대기 → 1_txt (원본은 pdf/ 보관)":
        "Convert PDF/TXT to text · upload queue → 1_txt (original kept in pdf/)",
    "📂 장분할 앱": "📂 Chapter Splitter",
    "책 TXT를 챕터 단위로 분리 · 1_txt → chapters":
        "Split book text into chapters · 1_txt → chapters",
    "🌐 영문번역 앱": "🌐 Translator",
    "챕터를 한국어로 번역 · chapters → 번역본(_ko.txt)":
        "Translate chapters into Korean · chapters → _ko.txt",
    "📝 문서요약 앱": "📝 Summarizer",
    "챕터별 요약 노트 생성 · chapters → 요약(_wiki.md)":
        "Generate per-chapter summaries · chapters → _wiki.md",
    "📖 위키반영 앱": "📖 Wiki Publisher",
    "요약을 Obsidian 노트로 저장 · 요약(_wiki.md) → 보관함(Vault)":
        "Publish summaries as Obsidian notes · _wiki.md → Vault",
    "⚙️ 설정": "⚙️ Settings",
    "API 키와 위키 생성 모델 설정": "API keys, wiki model and language",
    "🚀 전체 실행": "🚀 Run All",
    "TXT변환 → 장분할 → 번역 → 요약 → Wiki를 한 번에 실행":
        "Run convert → split → translate → summarize → wiki in one go",
    "🧭 메뉴": "🧭 Menu",
    "📄 TXT변환": "📄 Convert",
    "📂 장분할": "📂 Split",
    "🌐 영문번역": "🌐 Translate",
    "📝 문서요약": "📝 Summarize",
    "📖 위키반영": "📖 Publish",

    # ── 저장 위치 / 공용 ─────────────────────────────────
    "📁 저장 위치": "📁 Storage locations",
    "열기": "Open",
    "보기": "View",
    "✅ 전체 선택": "✅ Select all",
    "⬜ 해제": "⬜ Clear",
    "총 %d개": "Total %d",
    "다음 단계": "Next step",
    "결과 폴더 열기": "Open result folder",
    "닫기": "Close",
    "완료": "Done",
    "📂 폴더 열기": "📂 Open folder",
    "번역 엔진": "Translation engine",
    "🤖 AI 모델": "🤖 AI model",
    "사용 가능한 AI 없음 — ⚙️ 설정 탭에서 API 키를 입력하세요.":
        "No AI available — enter an API key in ⚙️ Settings.",
    "정렬": "Sort",
    "최근 추가순": "Newest first",
    "이름순": "By name",
    "검색어 입력…": "Search…",
    "🗑 큐 비우기": "🗑 Clear queue",

    # ── 흐름 패널 ─────────────────────────────────────────
    "📄 TXT변환 앱 (Text Converter)": "📄 Text Converter",
    "PDF의 텍스트 레이어를 추출해 TXT로 저장합니다 (OCR 변환된 문서만 가능). 원본 PDF는 pdf/ 폴더에 보관됩니다.":
        "Extracts the text layer of a PDF and saves it as TXT (text-based PDFs only). The original PDF is kept in the pdf/ folder.",
    "① 처리전 · 업로드 대기": "① Input · Upload queue",
    "② 처리후 · 변환 TXT": "② Output · Converted TXT",
    "📄 원본 PDF 보관": "📄 Original PDFs",
    "%d개 대기": "%d waiting",
    "%d권 변환됨": "%d converted",
    "%d개 보관": "%d archived",
    "📂 장분할 앱 (Chapter Splitter)": "📂 Chapter Splitter",
    "책 TXT를 장(Chapter) 단위 파일로 분리해 책별 폴더에 저장합니다.":
        "Splits book text into one file per chapter, stored in a folder per book.",
    "① 처리전 · 변환 TXT": "① Input · Converted TXT",
    "② 처리후 · 챕터 폴더": "② Output · Chapter folders",
    "%d권": "%d books",
    "%d권 분할됨": "%d split",
    "🌐 영문번역 앱 (Translator)": "🌐 Translator",
    "챕터 TXT를 한국어로 번역해 같은 폴더에 `_ko.txt`로 저장합니다.":
        "Translates chapter text into Korean, saved as `_ko.txt` in the same folder.",
    "① 처리전 · 원문 챕터": "① Input · Source chapters",
    "② 처리후 · 번역본 (_ko.txt)": "② Output · Translations (_ko.txt)",
    "%d개 번역됨": "%d translated",
    "📝 문서요약 앱 (Summarizer)": "📝 Summarizer",
    "챕터 TXT(번역본 우선)로 요약을 생성해 같은 폴더에 `_wiki.md`로 저장합니다. 위키반영 전에 열어서 손으로 고칠 수 있습니다.":
        "Generates a summary per chapter (translation preferred), saved as `_wiki.md`. You can open and edit it before publishing.",
    "① 처리전 · 챕터 (번역본 우선)": "① Input · Chapters (translation preferred)",
    "② 처리후 · 요약 (_wiki.md)": "② Output · Summaries (_wiki.md)",
    "원문 %d · 번역 %d": "source %d · translated %d",
    "%d개 요약됨": "%d summarized",
    "📖 위키반영 앱 (Wiki Publisher)": "📖 Wiki Publisher",
    "챕터 요약(_wiki.md)들을 합쳐 Obsidian 보관함(Vault)에 위키 노트로 저장합니다.":
        "Combines chapter summaries (_wiki.md) into wiki notes in your Obsidian vault.",
    "① 처리전 · 요약 (_wiki.md)": "① Input · Summaries (_wiki.md)",
    "② 처리후 · Obsidian 보관함": "② Output · Obsidian vault",
    "%d노트": "%d notes",

    # ── 1: TXT변환 ───────────────────────────────────────
    "처리 모드": "Mode",
    "📄 TXT저장만": "📄 Convert to TXT only",
    "🚀 전체 실행 (TXT변환→장별분할→번역(영어문서인 경우)→Wiki)":
        "🚀 Run all (convert → split → translate if English → wiki)",
    "번역 엔진 없음 — ⚙️ 설정 탭에서 API 키를 입력하세요.":
        "No translation engine — enter an API key in ⚙️ Settings.",
    "PDF 또는 TXT 업로드 (여러 파일 가능)": "Upload PDF or TXT (multiple files allowed)",
    "📥 처리 대기 목록에 추가됨: %s": "📥 Added to queue: %s",
    "🔎 논문 출처로 가져오기": "🔎 Fetch from paper source",
    "논문 출처": "Paper source",
    "URL, DOI(10.xxxx/...), doi:..., arXiv 번호 또는 arxiv.org 링크":
        "URL, DOI (10.xxxx/...), doi:..., arXiv id or arxiv.org link",
    "다운로드 확인 후 TXT 저장": "Verify download and save as TXT",
    "논문 출처 확인 중…": "Checking paper source…",
    "(%s) 때문에 가져올 수 없습니다.": "Could not fetch: %s",
    "✅ 다운로드 가능: `%s`": "✅ Downloadable: `%s`",
    "✅ TXT 저장 완료: %s": "✅ Saved as TXT: %s",
    "📄 원본 PDF 보관: `%s`": "📄 Original PDF stored: `%s`",
    "(%s) 때문에 TXT로 저장할 수 없습니다.": "Could not save as TXT: %s",
    "**🔎 최근 가져온 논문:** %s": "**🔎 Recently fetched paper:** %s",
    "✕ 닫기": "✕ Close",
    "📂 위치 열기": "📂 Reveal",
    "📝 변환 TXT: %s": "📝 Converted TXT: %s",
    "📄 원본 PDF: %s": "📄 Original PDF: %s",
    "— (TXT 출처라 PDF 없음)": "— (source was TXT, no PDF)",
    "#### 처리 대기 (%d개)": "#### Queue (%d)",
    "▶ 선택 처리 (%d개)": "▶ Process selected (%d)",
    "▶ 전체 처리 (%d개)": "▶ Process all (%d)",
    "대기 중인 파일 없음 — 위에서 PDF를 업로드하세요.": "Queue is empty — upload a PDF above.",
    "#### 완료 기록 (%d권)": "#### Completed (%d)",
    "해당 폴더에 완료된 TXT 없음": "No completed TXT in this folder",
    "⚠️ 실패 %d건": "⚠️ Failed: %d",
    "1-TXT변환 완료": "Text conversion finished",
    "%d개 파일 처리를 마쳤습니다. 다음 단계에서 장별 분할을 진행하세요.":
        "Processed %d file(s). Continue with chapter splitting in the next step.",
    "💡 다음 단계: **📂 장분할 앱**으로 이동하세요": "💡 Next: go to **📂 Chapter Splitter**",

    # ── 2: 장분할 ────────────────────────────────────────
    "TXT 직접 업로드 (done/ 폴더로 저장)": "Upload TXT directly (saved into done/)",
    "%d개 TXT 저장 완료": "Saved %d TXT file(s)",
    "#### 분할 대기 (%d권)": "#### Split queue (%d)",
    "🤖 장 구조 감지에 설정된 AI 모델이 사용될 수 있습니다 (PDF 시각 판독·비정형 헤딩) — 현재: %s":
        "🤖 The configured AI model may be used to detect chapter structure (PDF visual reading, irregular headings) — current: %s",
    "개 챕터": " chapters",
    "🤖 AI 모델 사용됨: %s": "🤖 AI model used: %s",
    "📑 PDF 북마크": "📑 PDF bookmarks",
    "🤖 AI 시각판독": "🤖 AI visual reading",
    "패턴(MD 헤딩)": "pattern (MD headings)",
    "패턴(목차 복원)": "pattern (TOC reconstruction)",
    "패턴(번호 헤딩)": "pattern (numbered headings)",
    "🤖 AI 텍스트판정": "🤖 AI text analysis",
    "단일 본문": "single body",
    "(한국어 책 — 번역 생략, 문서요약으로 이동)":
        "(Korean book — translation skipped, continue to Summarizer)",
    "▶ 선택 분할 (%d권)": "▶ Split selected (%d)",
    "▶ 전체 분할 (%d권)": "▶ Split all (%d)",
    "짧은 문서가 감지되었습니다. 아래 '짧은 문서 확인'에서 분리 또는 단일장 유지를 선택하세요.":
        "Short documents detected. Choose split or keep-as-one under 'Short documents' below.",
    "분할 대기 없음 — 📄 TXT변환 앱에서 TXT를 먼저 생성하거나 아래에서 수동 추가하세요":
        "Nothing to split — create TXT in 📄 Text Converter first, or add manually below",
    "#### 짧은 문서 확인 (%d권)": "#### Short documents (%d)",
    "짧은 문서는 먼저 확인한 뒤, 실제 분리하거나 단일장으로 유지할 수 있습니다.":
        "Review short documents, then either split them or keep each as a single chapter.",
    "분리하기": "Split",
    "단일장 유지": "Keep as one",
    "#### 장 구조 미감지 (%d권)": "#### No chapter structure (%d)",
    "장 헤딩을 찾지 못한 문서입니다. 통째로 번역·요약하려면 단일장으로 저장하세요.":
        "No chapter headings were found. Save as a single chapter to translate/summarize whole.",
    "📄 단일장으로 저장": "📄 Save as single chapter",
    "➕ 수동으로 추가 (기존 책에서 선택)": "➕ Add manually (from existing books)",
    "책 이름 검색": "Search book title",
    "➕ 선택 항목 큐에 추가 (%d권)": "➕ Add selected to queue (%d)",
    "#### 분할 완료 (%d권)": "#### Split done (%d)",
    "📂 열기": "📂 Open",
    "🌐 합친 번역본": "🌐 Merged translation",
    "완료된 분할 없음": "No completed splits",
    "2-장별분할 완료": "Chapter split finished",
    "%d권 분할을 마쳤습니다. 다음 단계로 이동하세요.":
        "Split %d book(s). Continue to the next step.",
    "💡 다음 단계: **🌐 영문번역 앱**으로 이동하세요": "💡 Next: go to **🌐 Translator**",

    # ── 3: 번역 ─────────────────────────────────────────
    "TXT 직접 업로드 (즉시 번역)": "Upload TXT directly (translate now)",
    "⏸️ 번역 중 다른 버튼을 누르거나 화면을 이동하면 중단됩니다 — 진행분은 `_ko.partial.md`로 저장되고, 같은 챕터를 다시 실행하면 이어서 번역합니다.":
        "⏸️ Clicking anything or navigating during translation interrupts it — progress is kept in `_ko.partial.md`, and re-running the chapter resumes where it stopped.",
    "#### 번역 대기 (%d개) / 완료 %d개": "#### Translation queue (%d) / done %d",
    " · ♻️ 중단됨 — 이어하기 가능": " · ♻️ interrupted — resumable",
    "▶ 선택 번역 (%d개)": "▶ Translate selected (%d)",
    "▶ 전체 번역 (%d개)": "▶ Translate all (%d)",
    "번역 대기 없음 — 📂 장분할 앱에서 챕터를 먼저 분리하세요":
        "Nothing to translate — split chapters in 📂 Chapter Splitter first",
    "➕ 수동으로 추가 (기존 챕터에서 선택)": "➕ Add manually (from existing chapters)",
    "책/챕터 이름 검색": "Search book/chapter",
    "➕ 선택 항목 큐에 추가 (%d개)": "➕ Add selected to queue (%d)",
    "3-영문번역 완료": "Translation finished",
    "%d개 챕터 번역을 마쳤습니다. 다음 단계에서 요약 MD를 생성하세요.":
        "Translated %d chapter(s). Generate summaries in the next step.",
    "💡 다음 단계: **📝 문서요약 앱**으로 이동하세요": "💡 Next: go to **📝 Summarizer**",

    # ── 4: 요약 ─────────────────────────────────────────
    "요약 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요.":
        "No summarization API — enter a key in ⚙️ Settings.",
    "TXT 직접 업로드 (즉시 요약)": "Upload TXT directly (summarize now)",
    "#### 요약 대기 (%d개) / 완료 %d개": "#### Summary queue (%d) / done %d",
    "▶ 선택 요약 (%d개)": "▶ Summarize selected (%d)",
    "▶ 전체 요약 (%d개)": "▶ Summarize all (%d)",
    "요약 대기 없음 — 🌐 영문번역 앱 처리 후 자동 등록되거나 아래에서 수동 추가하세요":
        "Nothing to summarize — chapters register automatically after 🌐 Translator, or add manually below",
    "#### 요약 실패 (%d개)": "#### Failed summaries (%d)",
    "📚 책 전체요약 생성: %s": "📚 Generating book overview: %s",
    "전체요약 %s: %s": "Overview %s: %s",
    "📚 책 전체요약 (<책제목>_전체요약.md) — %d권": "📚 Book overviews (<title>_전체요약.md) — %d",
    "장별 요약을 합쳐 만든 책 전체 요약입니다. 위키반영 전에 열어서 고칠 수 있고, 수정본이 허브 노트에 그대로 반영됩니다.":
        "A whole-book overview built from the chapter summaries. You can edit it before publishing; your edits go into the hub note as-is.",
    "✅ 있음": "✅ exists",
    "— 없음": "— none",
    "↻ 재생성": "↻ Regenerate",
    "▶ 생성": "▶ Generate",
    "↻ 선택 재시도 대기 (%d개)": "↻ Retry selected (%d)",
    "🗑 실패 목록 비우기": "🗑 Clear failed list",
    "4-문서요약 완료": "Summaries finished",
    "%d개 챕터 요약을 마쳤습니다. 다음 단계에서 Wiki 반영을 진행하세요.":
        "Summarized %d chapter(s). Publish to the wiki in the next step.",
    "💡 다음 단계: **📖 위키반영 앱**으로 이동하세요": "💡 Next: go to **📖 Wiki Publisher**",

    # ── 5: 위키반영 ──────────────────────────────────────
    "📁 위키 저장 보관함(Vault): `%s`  (`%s`)": "📁 Wiki vault: `%s`  (`%s`)",
    "Obsidian 보관함(Vault) 선택": "Choose Obsidian vault",
    "✅ 이 보관함(Vault)로 변경 (즉시 적용)": "✅ Use this vault (applies now)",
    "또는 직접 경로 입력": "Or enter a path directly",
    "✅ 직접 입력 경로로 변경 (즉시 적용)": "✅ Use entered path (applies now)",
    "Obsidian 보관함(Vault) 목록을 가져올 수 없습니다. Obsidian이 설치·실행됐는지 확인하세요.":
        "Could not read the Obsidian vault list. Check that Obsidian is installed and has been run.",
    "Wiki 생성 API 없음 — ⚙️ 설정 탭에서 키를 입력하세요.":
        "No wiki-generation API — enter a key in ⚙️ Settings.",
    "#### 챕터 요약 → Wiki (%d권 대기)": "#### Summaries → Wiki (%d waiting)",
    "**책 제목**": "**Book title**",
    "챕터": "chapters",
    "%d/%d챕터 요약됨": "%d/%d chapters summarized",
    "▶ 선택 Wiki생성 (%d권)": "▶ Publish selected (%d)",
    "▶ 전체 Wiki생성 (%d권)": "▶ Publish all (%d)",
    "Wiki 대기 없음 — 📝 문서요약 앱에서 요약 완료 후 자동 등록되거나 아래에서 수동 추가하세요":
        "Nothing to publish — books register automatically after 📝 Summarizer, or add manually below",
    "➕ 수동으로 추가 (요약 완료된 책에서 선택)": "➕ Add manually (from summarized books)",
    "%d챕터 요약": "%d chapter summaries",
    "#### 단일 TXT → Wiki (%d권 · 챕터 분할 없음)": "#### Single TXT → Wiki (%d · no chapter split)",
    "전체 TXT를 Gemini에 넣어 백그라운드로 단일 위키 노트 생성":
        "Feeds the whole TXT to Gemini and builds a single wiki note in the background",
    "▶ 선택 단일 Wiki (%d권)": "▶ Publish selected as single note (%d)",
    "#### Wiki 완료 (%d노트)": "#### Wiki notes (%d)",
    "📓 Obsidian 보관함(Vault) 열기": "📓 Open Obsidian vault",
    "5-Wiki 반영 완료": "Wiki publish finished",
    "%d권 Wiki 반영을 마쳤습니다.": "Published %d book(s) to the wiki.",

    # ── 설정 ─────────────────────────────────────────────
    "🌐 언어 / Language": "🌐 언어 / Language",
    "한국어": "한국어",
    "English": "English",
    "API 키는 이 화면에서 직접 저장한 값만 사용합니다. 저장 키는 `~/.config/mybookshelf/keys.json`에만 보관되며 저장소에 올라가지 않습니다.":
        "Only API keys saved on this screen are used. Keys are stored in `~/.config/mybookshelf/keys.json` and never leave this machine.",
    "저작권 및 사용 주의": "Copyright and usage notice",
    "위키 노트를 생성할 모델": "Model for wiki note generation",
    "사용 가능한 API 키나 활성화된 CLI가 없습니다. 아래에서 API 키를 입력하거나 CLI 사용을 켜세요.":
        "No API key or active CLI. Enter an API key below or enable a CLI tool.",
    "💾 저장": "💾 Save",
    "🗑 삭제": "🗑 Delete",
    "저장됨": "Saved",
    "키를 입력하세요.": "Enter a key.",
    "미설정": "not set",
}
