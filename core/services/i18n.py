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
    "⚠️ 일부 문서는 처리 중 특이사항이 있었습니다 (자동 보정됨):":
        "⚠️ Some documents had issues during processing (auto-corrected):",
    "업데이트": "Updates",
    "현재 버전: %s": "Current version: %s",
    "업데이트 확인": "Check for updates",
    "최신 버전을 사용 중입니다.": "You are on the latest version.",
    "앱 내 업데이트는 Windows에서만 지원됩니다.": "In-app updates are supported on Windows only.",
    "짧은 문서는 챕터로 나누기 애매합니다. 각 문서를 '보기'로 확인한 뒤, 아래에서 분할 처리·다음 단계 이동·삭제를 선택하세요.":
        "Short documents are hard to split into chapters. Preview each with 'View', then choose Split / Move to next / Delete below.",
    "삭제 (%d권)": "Delete (%d)",
    "%d권을 챕터로 분할했습니다.": "Split %d document(s) into chapters.",
    "%d건을 단일장으로 저장해 다음 단계로 보냈습니다.": "Saved %d document(s) as a single chapter and sent to the next step.",
    "%d개 문서를 삭제했습니다.": "Deleted %d document(s).",
    "업데이트 사용 가능": "Update available",
    "새 버전 **%s** 이(가) 나왔습니다. (현재 %s)": "A new version **%s** is available. (current %s)",
    "변경 내용 보기": "View changes",
    "업데이트하면 앱이 닫혔다가 자동으로 다시 열립니다.":
        "The app will close and reopen automatically after updating.",
    "지금 업데이트": "Update now",
    "브라우저로 받기": "Download in browser",
    "나중에": "Later",
    "설치 파일을 내려받는 중입니다…": "Downloading the installer…",
    "자동 업데이트 실패": "Automatic update failed",
    "아래 '브라우저로 받기'로 직접 내려받아 설치해 주세요.":
        "Please download and install manually via 'Download in browser' below.",
    "다운로드 완료 — 앱을 닫고 업데이트를 설치합니다. 잠시 후 자동으로 다시 열립니다.":
        "Download complete — closing the app to install. It will reopen automatically shortly.",
    "업데이트 실행에 실패했습니다.": "Failed to start the update.",
    "OCR 필요": "OCR required",
    "OCR 사전 처리가 필요합니다": "OCR preprocessing required",
    "다음 문서는 이미지로만 되어 있어, TXT 분리를 위해서는 OCR 사전 처리 작업이 필요합니다:":
        "These documents are image-only. OCR preprocessing is required before text extraction:",
    "⚠️ 다음 %d개 문서는 이미지로만 되어 있어 OCR 사전 처리가 필요합니다: %s":
        "⚠️ %d document(s) are image-only and need OCR preprocessing first: %s",
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
    "단락 %d/%d · 재사용 %d · API 호출 %d · 번역 %d · 보존 %d · 제외 %d · 실패 %d":
        "Paragraph %d/%d · reused %d · API calls %d · translated %d · preserved %d · dropped %d · failed %d",
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
    "📦 분할이 끝난 원본 TXT는 완료 보관 폴더(%s)로 이동합니다 — 보관·열람용이며, 더 이상 필요 없으면 직접 삭제해도 됩니다.":
        "📦 After splitting, the source TXT moves to the archive folder (%s) — kept for reference; you may delete it manually when no longer needed.",
    "✅ 완료 보관 (원본 TXT)": "✅ Archive (source TXT)",
    "%d권 보관": "%d archived",
    "📦 원본 TXT → 완료 보관 폴더로 이동 (보관용)": "📦 Source TXT moved to the archive folder",
    "요약 준비 중…": "Preparing summaries…",
    "요약 %d/%d — %s": "Summarizing %d/%d — %s",
    "전체요약 ✓": "overview ✓",
    "전체요약 — (반영 시 자동 생성)": "overview — (auto-generated on publish)",
    "📚 허브 노트 생성 — 책 전체요약 + 챕터 링크·요약 포함":
        "📚 Building hub note — includes book overview + chapter links/summaries",
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
    "#### 새 요약 있음 · 기존 Wiki 갱신 확인 (%d권)": "#### New summaries · Confirm existing Wiki updates (%d)",
    "기존 Wiki가 있습니다. 명시적으로 선택한 책만 새 요약으로 다시 반영합니다. 선택하지 않은 책은 기존 노트를 유지합니다.":
        "An existing Wiki was found. Only explicitly selected books will be updated with the new summaries; unselected books keep their existing notes.",
    "다시 반영 (%d권)": "Publish again (%d)",
    "이번 갱신 건너뛰기 (%d권)": "Skip this update (%d)",
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

# ── v0.9.2~v0.9.8 UI(버튼·메뉴·제목·설명) 영어 번역 보강 (2026-07-10) ──
_EN.update({
    # 상단 지표
    "AI 구독(CLI)": "AI subscription (CLI)",
    "AI API 키": "AI API keys",
    "✕ 없음": "✕ none",
    "생성 중": "running",
    # 내비 · 메뉴 · 탭 제목
    "메뉴": "Menu",
    "텍스트 변환": "Text",
    "챕터 분할": "Chapter split",
    "영문번역": "Translation",
    "문서요약": "Summaries",
    "위키반영": "Wiki",
    "설정": "Settings",
    ":material/description: 텍스트 변환": ":material/description: Text conversion",
    ":material/content_cut: 챕터 분할": ":material/content_cut: Chapter split",
    ":material/translate: 영문번역": ":material/translate: Translation",
    ":material/summarize: 문서요약": ":material/summarize: Summaries",
    ":material/menu_book: 위키반영": ":material/menu_book: Wiki",
    # 탭 설명
    "PDF의 텍스트 레이어를 추출해 TXT로 저장합니다 (OCR 변환된 문서만 가능).":
        "Extracts the text layer of a PDF and saves it as TXT (text-based PDFs only).",
    "책 TXT를 챕터(Chapter) 단위 파일로 분리해 책별 폴더에 저장합니다.":
        "Splits a book TXT into per-chapter files under a per-book folder.",
    "챕터 TXT를 한국어로 번역해 같은 폴더에 `_ko.txt`로 저장합니다.":
        "Translates chapter TXT into Korean, saved as `_ko.txt` in the same folder.",
    "챕터 TXT(번역본 우선)로 요약을 생성해 같은 폴더에 `_wiki.md`로 저장합니다.":
        "Summarizes chapter TXT (translation preferred), saved as `_wiki.md`.",
    "챕터 요약(_wiki.md)들을 합쳐 Obsidian 보관함(Vault)에 위키 노트로 저장합니다.":
        "Merges chapter summaries (_wiki.md) into Wiki notes in the Obsidian vault.",
    "PDF/TXT를 텍스트로 변환 · 업로드 대기 → 변환 TXT":
        "Convert PDF/TXT to text · upload queue → converted TXT",
    "책 TXT를 챕터 단위로 분리 · 변환 TXT → chapters":
        "Split a book TXT into chapters · converted TXT → chapters",
    "챕터를 한국어로 번역 · chapters → 번역본(_ko.txt)":
        "Translate chapters to Korean · chapters → translation (_ko.txt)",
    "챕터별 요약 노트 생성 · chapters → 요약(_wiki.md)":
        "Create per-chapter summaries · chapters → summary (_wiki.md)",
    "요약을 Obsidian 노트로 저장 · 요약(_wiki.md) → 보관함(Vault)":
        "Save summaries as Obsidian notes · summary (_wiki.md) → vault",
    "API 키와 위키 생성 모델 설정": "Set API keys and the Wiki model",
    # 흐름 카드 라벨
    "① 처리전 · 업로드 대기": "① Before · upload queue",
    "② 처리후 · 변환 TXT": "② After · converted TXT",
    "📄 원본 PDF 보관": "📄 Original PDF store",
    "① 처리전 · 변환 TXT": "① Before · converted TXT",
    "② 처리후 · 챕터 폴더": "② After · chapter folders",
    "✅ 완료 보관 (원본 TXT)": "✅ Archived (source TXT)",
    "① 처리전 · 원문 챕터": "① Before · source chapters",
    "② 처리후 · 번역본 (_ko.txt)": "② After · translation (_ko.txt)",
    "① 처리전 · 챕터 (번역본 우선)": "① Before · chapters (translation first)",
    "② 처리후 · 요약 (_wiki.md)": "② After · summaries (_wiki.md)",
    "① 처리전 · 요약 (_wiki.md)": "① Before · summaries (_wiki.md)",
    "② 처리후 · Obsidian 보관함": "② After · Obsidian vault",
    # 개수 문구
    "%d개 대기": "%d waiting",
    "%d권 변환됨": "%d converted",
    "%d개 보관": "%d stored",
    "%d권": "%d books",
    "%d권 분할됨": "%d split",
    "%d권 보관": "%d archived",
    "%d개 번역됨": "%d translated",
    "원문 %d · 번역 %d": "source %d · translated %d",
    "%d개 요약됨": "%d summarized",
    "%d노트": "%d notes",
    "총 %d권": "%d total",
    # 버튼 (동작)
    "시작 (%d개)": "Start (%d)",
    "시작 (%d권)": "Start (%d)",
    "중단": "Stop",
    "삭제": "Delete",
    "삭제 (%d개)": "Delete (%d)",
    "삭제 (%d권)": "Delete (%d)",
    "분할 처리": "Split",
    "분할 처리 (%d권)": "Split (%d)",
    "다음단계로 이동": "Move to next step",
    "다음단계로 이동 (%d권)": "Move to next step (%d)",
    "텍스트 변환 처리 (%d개)": "Convert to text (%d)",
    "전체 선택": "Select all",
    "해제": "Clear",
    "폴더 열기": "Open folder",
    "위치 열기": "Open location",
    "재생성": "Regenerate",
    "생성": "Generate",
    "저장": "Save",
    "합친 번역본": "Merged translation",
    "단일장으로 저장": "Save as single chapter",
    "선택 항목 큐에 추가 (%d개)": "Add selected to queue (%d)",
    "선택 항목 큐에 추가 (%d권)": "Add selected to queue (%d)",
    "선택 재시도 대기 (%d개)": "Requeue selected (%d)",
    "실패 목록 비우기": "Clear failed list",
    "Wiki 생성 (%d권)": "Build Wiki (%d)",
    "선택 단일 Wiki (%d권)": "Build single Wiki (%d)",
    "이 모델로 위키 생성": "Use this model for Wiki",
    "이 보관함(Vault)로 변경 (즉시 적용)": "Switch to this vault (apply now)",
    "직접 입력 경로로 변경 (즉시 적용)": "Switch to entered path (apply now)",
    "위키 보관함(Vault) 저장 (즉시 적용)": "Save Wiki vault (apply now)",
    "Obsidian 보관함(Vault) 열기": "Open Obsidian vault",
    # 아이콘 전용 버튼 help
    "다시 합치기": "Merge again",
    "재분할": "Re-split",
    "재시도": "Retry",
    "목록에서 제거": "Remove from list",
    "글자 크기 줄이기": "Decrease font size",
    "글자 크기 키우기": "Increase font size",
    # 처리 화면(런패널)
    "%d/%d 처리 중": "Processing %d/%d",
    "챕터 분할 처리 중": "Splitting chapters",
    "영문번역 처리 중": "Translating",
    "문서요약 처리 중": "Summarizing",
    "위키반영 처리 중": "Building Wiki",
    "처리 중에는 다른 기능이 잠깁니다. '중단'을 누르면 현재 항목까지 마친 뒤 멈추고, 남은 작업은 다시 '시작'으로 이어집니다.":
        "Other actions are locked while processing. 'Stop' finishes the current item then halts; press 'Start' again to resume the rest.",
    # 섹션 헤더
    "### :material/hub: AI 구독 (CLI)": "### :material/hub: AI subscription (CLI)",
    "### :material/key: API 키 등록": "### :material/key: API keys",
    "### :material/book_2: 옵시디언(Obsidian) 보관함 설정": "### :material/book_2: Obsidian vault settings",
    "### ⚠️ 짧은 문서 확인 (%d권)": "### ⚠️ Short documents (%d)",
    # 설정 · AI 모델
    "AI 모델은 설정에서 선택합니다 · 현재: ": "AI model is chosen in Settings · current: ",
    "API 키 없이 구독으로 사용 — 설치·로그인 후 켜세요. AI 키 등록보다 우선합니다.":
        "Use via subscription without an API key — install, log in, then enable. Takes priority over API keys.",
    "위키 노트를 생성할 모델": "Model for generating Wiki notes",
    "미설치": "not installed",
    "미설치 · `npm i -g @anthropic-ai/claude-code`": "not installed · `npm i -g @anthropic-ai/claude-code`",
    "미설치 · `npm i -g @openai/codex`": "not installed · `npm i -g @openai/codex`",
    # 안내(대기 없음 등)
    "번역 대기 없음 — 📂 챕터 분할에서 챕터를 먼저 분리하세요":
        "No translation queue — split chapters in 📂 Chapter split first",
    "분할 대기 없음 — 📄 텍스트 변환에서 TXT를 먼저 생성하거나 아래에서 수동 추가하세요":
        "No split queue — create TXT in 📄 Text first, or add manually below",
    "요약 대기 없음 — 🌐 영문번역 처리 후 자동 등록되거나 위에서 TXT를 직접 업로드하세요":
        "No summary queue — auto-added after 🌐 Translation, or upload TXT above",
    "Wiki 대기 없음 — 📝 문서요약에서 요약 완료 후 자동 등록되거나 아래에서 수동 추가하세요":
        "No Wiki queue — auto-added after 📝 Summaries, or add manually below",
    "번역 대기에 %d개 등록됨 — 아래에서 [▶ 시작]": "Queued %d for translation — press [▶ Start] below",
    "요약 대기에 %d개 등록됨 — 아래에서 [▶ 시작]": "Queued %d for summary — press [▶ Start] below",
    "업로드한 TXT는 아래 '번역 대기'에 등록됩니다. [▶ 시작]을 눌러야 번역이 시작됩니다.":
        "Uploaded TXT is added to the translation queue below. Press [▶ Start] to begin.",
    "업로드한 TXT는 아래 '요약 대기'에 등록됩니다. [▶ 시작]을 눌러야 요약이 시작됩니다.":
        "Uploaded TXT is added to the summary queue below. Press [▶ Start] to begin.",
    "⚠️ 짧은 문서가 감지되었습니다. 아래 '짧은 문서 확인'에서 분할 처리 또는 다음단계로 이동을 선택하세요.":
        "⚠️ Short documents detected. In 'Short documents' below, choose Split or Move to next step.",
    "짧은 문서는 챕터로 나누기 애매합니다. 챕터로 분할하거나, 통째로 다음 단계(영문→영문번역·한글→문서요약)로 보낼 수 있습니다.":
        "Short docs are hard to split. Split into chapters, or send whole to the next step (EN→Translation, KO→Summaries).",
    "분할 없이 단일장으로 저장하고 영문은 영문번역, 한글은 문서요약으로 이동":
        "Save as a single chapter without splitting; EN→Translation, KO→Summaries",
    "아직 위키로 만들지 않은 단일 TXT입니다. 위키로 만들거나, 필요 없으면 원본 TXT를 삭제할 수 있습니다.":
        "Single TXTs not yet turned into Wiki. Build a Wiki, or delete the source TXT if not needed.",
    "TXT 직접 업로드": "Upload TXT directly",
    "📎 파일 선택 또는 이 영역으로 끌어다 놓기(Drag & Drop) 가능":
        "📎 Pick a file or drag & drop it here",
    # 완료 메시지 · 라우팅
    "2-챕터 분할 완료": "2 · Chapter split done",
    "2-단일장 저장 완료": "2 · Saved as single chapter",
    "분할을 마쳤습니다.": "Splitting done.",
    "%d건을 다음 단계로 보냈습니다.": "Sent %d to the next step.",
    "번역을 마쳤습니다. 다음 단계에서 요약을 생성하세요.": "Translation done. Generate summaries next.",
    "요약을 마쳤습니다. 다음 단계에서 Wiki 반영을 진행하세요.": "Summaries done. Build the Wiki next.",
    "Wiki 반영을 마쳤습니다.": "Wiki build done.",
    "영문 → 영문번역": "EN → Translation",
    "한글 → 문서요약": "KO → Summaries",
    "영문 문서 → 영문번역": "EN document → Translation",
    "한글 문서 → 문서요약": "KO document → Summaries",
    # 저작권/주의
    "API 키는 이 화면에서 직접 저장한 값만 사용합니다. ":
        "Only keys saved on this screen are used. ",
    "저장 키는 `~/.config/mybookshelf/keys.json`에만 보관되며 저장소에 올라가지 않습니다.":
        "Saved keys are stored only in `~/.config/mybookshelf/keys.json` and never committed.",
    # 다음 단계 안내 · 폴더 · 기타
    "📁 폴더 열기": "📁 Open folder",
    "💡 다음 단계: **📂 챕터 분할**으로 이동하세요": "💡 Next: go to **📂 Chapter split**",
    "💡 다음 단계: **🌐 영문번역**으로 이동하세요": "💡 Next: go to **🌐 Translation**",
    "💡 다음 단계: **📝 문서요약**으로 이동하세요": "💡 Next: go to **📝 Summaries**",
    "💡 다음 단계: **📖 위키반영**으로 이동하세요": "💡 Next: go to **📖 Wiki**",
    "영문 책 → 영문번역": "EN book → Translation",
    "한글 책 → 문서요약": "KO book → Summaries",
    "%s 을(를) 단일장으로 저장했습니다.": "Saved %s as a single chapter.",
    "TXT 내용이 비어 있습니다.": "TXT content is empty.",
    "TXT/MD 내용이 비어 있습니다.": "TXT/MD content is empty.",
    "TXT/MD 파일이 없습니다.": "No TXT/MD file.",
    "기존 단일장 파일을 이어서 사용합니다.": "Reusing the existing single-chapter file.",
    "기존 장 파일을 다시 사용했습니다.": "Reused the existing chapter file.",
    "단일장 파일 생성에 실패했습니다.": "Failed to create the single-chapter file.",
    "단일장 파일을 저장했습니다.": "Saved the single-chapter file.",
    "이미 여러 장으로 분할된 책입니다. 2-장별분할 탭에서 처리하세요.":
        "This book is already split into chapters. Use the Chapter split tab.",
    "💡 URL이 잘 안 될 때: ① 로그인·구독이 필요한 페이지(대학도서관·유료 저널)나 ":
        "💡 If the URL fails: ① Pages needing login/subscription (library, paywalled journals) or ",
    "본문이 아닌 소개 페이지 링크는 받아올 수 없습니다 — PDF를 내려받아 위에서 직접 업로드하세요. ":
        "landing-page links (not the full text) can't be fetched — download the PDF and upload it above. ",
    "② DOI(10.xxxx/…)나 arXiv 번호(예: 2412.12107)가 있으면 그 값을 넣는 편이 가장 안정적입니다. ":
        "② A DOI (10.xxxx/…) or arXiv number (e.g. 2412.12107) is the most reliable input. ",
    "③ 링크 끝이 `.pdf`인 직접 주소를 쓰세요. ④ 그래도 안 되면 브라우저에서 PDF를 저장한 뒤 업로드하는 방법이 가장 확실합니다.":
        "③ Use a direct URL ending in `.pdf`. ④ Otherwise, saving the PDF in your browser and uploading it is the surest way.",
    "처음 사용 전 확인: 이 앱은 사용자가 제공한 PDF/TXT를 정리, 번역, 요약, 위키 노트로 재구성하는 개인 작업 도구입니다. ":
        "Before first use: this app organizes, translates, summarizes, and restructures your own PDF/TXT into Wiki notes for personal use. ",
    "생성된 번역·요약·위키 노트의 정확성·완전성은 보장되지 않습니다. ":
        "Accuracy and completeness of generated translations, summaries and Wiki notes are not guaranteed. ",
    "원문 문서의 저작권·번역권·요약·재배포 가능 여부는 이용자 본인이 확인해야 합니다. ":
        "You are responsible for confirming the source document's copyright, translation, summary and redistribution rights. ",
    "AI API 또는 CLI 구독 도구를 활성화하면 문서 일부 또는 전체가 외부 AI 서비스로 전송됩니다. ":
        "Enabling an AI API or CLI tool sends part or all of the document to an external AI service. ",
    "**My Bookshelf** · © 2026 저작자 — 개인·비상업 연구 보조 용도. ":
        "**My Bookshelf** · © 2026 the author — for personal, non-commercial research use. ",
    # AI 없음 경고 (아이콘 모노톤화, 2026-07-10)
    "사용 가능한 AI가 없습니다 — :material/settings: 설정 탭에서 API 키를 입력하거나 CLI 구독 도구를 활성화하세요.":
        "No AI available — enter an API key or enable a CLI tool in the :material/settings: Settings tab.",
    "사용 가능한 AI 없음 — :material/settings: 설정 탭에서 API 키를 입력하세요.":
        "No AI available — enter an API key in the :material/settings: Settings tab.",
    "요약 API 없음 — :material/settings: 설정 탭에서 키를 입력하세요.":
        "No summary API — enter a key in the :material/settings: Settings tab.",
    "Wiki 생성 API 없음 — :material/settings: 설정 탭에서 키를 입력하세요.":
        "No Wiki API — enter a key in the :material/settings: Settings tab.",
    # 설정 · 위키 생성 모델 (2026-07-10)
    "위키 생성 모델": "Wiki model",
    "현재": "current",
    "번역과 별개로, 위키 노트 생성에 쓸 모델입니다. 구조화 출력은 공급자별로 자동 처리됩니다.":
        "The model used to generate Wiki notes (separate from translation). Structured output is handled per provider.",
    # 설정 · 요약 분량 슬라이더 (2026-07-23)
    "요약 분량": "Summary length",
    "요약 분량 (원문 대비 %)": "Summary length (% of source)",
    ":material/tune: 요약 분량 조절": ":material/tune: Adjust summary length",
    "설정 탭과 문서요약 탭이 같은 값을 공유합니다.": "The Settings tab and the Summary tab share the same value.",
    "장별 요약 본문을 원문 글자수 대비 몇 %로 만들지 정합니다 (권장 15%). 짧은 장은 최소 분량을 보장합니다. 다음 요약부터 적용됩니다.":
        "Sets each chapter note's body as a percentage of the source length (15% recommended). Short chapters keep a minimum length. Applies from the next summarization.",
    ":material/info: 분량(%)이 커질수록 생성되는 요약이 길어져 **출력 토큰 소비·API 비용이 늘어납니다.** (원문을 보내는 입력 토큰은 분량과 무관하게 동일합니다.)":
        ":material/info: A higher percentage produces longer summaries, so **output-token usage and API cost increase.** (Input tokens for sending the source text stay the same regardless of length.)",
})
