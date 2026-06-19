#!/usr/bin/env python3
import rumps
import subprocess
import urllib.request
import json
from pathlib import Path
from datetime import datetime

SSD          = Path("/Volumes/SSD_990EVOPlus")
WIKI_DIR     = SSD / "llm-wiki/wiki"
RAW_DIR      = SSD / "llm-wiki/raw"
PROCESSED    = RAW_DIR / "processed"
OLLAMA_URL   = "http://localhost:11434"
PIPELINE_URL = "http://localhost:8501"
PIPELINE_APP = str(SSD / "Thesis_SSD/pipeline_app.py")
PYTHON_BIN   = "/opt/homebrew/bin/python3"


def ollama_running():
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
        return True
    except Exception:
        return False


def streamlit_running():
    try:
        urllib.request.urlopen(PIPELINE_URL, timeout=2)
        return True
    except Exception:
        return False


def wiki_generator_running():
    r = subprocess.run(["pgrep", "-f", "wiki_generator.py"], capture_output=True)
    return r.returncode == 0


def count_raw_pending():
    if not RAW_DIR.exists():
        return 0
    return len([f for f in RAW_DIR.glob("*.txt")
                if not (PROCESSED / f.name).exists()])


def count_wiki():
    if not WIKI_DIR.exists():
        return 0
    return len(list(WIKI_DIR.rglob("*.md")))


_ICON = str(Path(__file__).resolve().parent / "menubar_icon.png")

class AIWikiApp(rumps.App):
    def __init__(self):
        super().__init__("", icon=_ICON, template=True, quit_button=None)
        self.menu = [
            rumps.MenuItem("── 상태 ──", callback=None),
            rumps.MenuItem("My Bookshelf: 확인 중..."),
            None,
            rumps.MenuItem("── 파일 현황 ──", callback=None),
            rumps.MenuItem("📄 위키 생성 대기: -"),
            rumps.MenuItem("📚 위키 완성: -"),
            None,
            rumps.MenuItem("My Bookshelf 시작", callback=self.open_pipeline),
            rumps.MenuItem("My Bookshelf 종료", callback=self.stop_pipeline),
            None,
            rumps.MenuItem("Obsidian 열기", callback=self.open_obsidian),
        ]
        self.timer = rumps.Timer(self.refresh, 10)
        self.timer.start()
        self.refresh(None)
        # 앱 실행 시 파이프라인 자동 시작
        if not streamlit_running():
            self._start_streamlit()

    def _start_streamlit(self):
        subprocess.Popen(
            [PYTHON_BIN, "-m", "streamlit", "run", PIPELINE_APP,
             "--server.port", "8501",
             "--server.headless", "true",
             "--browser.gatherUsageStats", "false"],
            stdout=open("/tmp/streamlit-pipeline.log", "a"),
            stderr=open("/tmp/streamlit-pipeline.err", "a"),
            start_new_session=True,
        )

    def _stop_streamlit(self):
        subprocess.run(["pkill", "-f", f"streamlit run {PIPELINE_APP}"],
                       capture_output=True)

    def refresh(self, _):
        st = streamlit_running()
        wg = wiki_generator_running()

        self.menu["My Bookshelf: 확인 중..."].title = f"My Bookshelf: {'✅ 실행중' if st else '❌ 중지'}"

        raw  = count_raw_pending()
        wiki = count_wiki()
        self.menu["📄 위키 생성 대기: -"].title = f"📄 위키 생성 대기: {raw}개"
        self.menu["📚 위키 완성: -"].title      = f"📚 위키 완성: {wiki}개"

        if wg:
            self.title = "↻"
        elif not st:
            self.title = "!"
        else:
            self.title = ""

    @rumps.clicked("My Bookshelf 시작")
    def open_pipeline(self, _):
        if not streamlit_running():
            self._start_streamlit()
            import threading, time
            def wait_and_open():
                for _ in range(10):
                    time.sleep(1)
                    if streamlit_running():
                        break
                subprocess.Popen(["open", PIPELINE_URL])
                self.refresh(None)
            threading.Thread(target=wait_and_open, daemon=True).start()
        else:
            subprocess.Popen(["open", PIPELINE_URL])

    @rumps.clicked("My Bookshelf 종료")
    def stop_pipeline(self, _):
        self._stop_streamlit()
        self.refresh(None)

    @rumps.clicked("Obsidian 열기")
    def open_obsidian(self, _):
        subprocess.Popen(["open", "-a", "Obsidian"])


if __name__ == "__main__":
    AIWikiApp().run()
