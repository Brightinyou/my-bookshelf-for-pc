# My Bookshelf

**A personal research tool that turns PDF/TXT documents into Obsidian Wiki notes** — Text conversion → Chapter split → (for English docs) Korean translation → Summaries → Obsidian Wiki, all in one flow.

> 🇰🇷 한국어 설명서: [README.md](README.md)

This repository shares one core (`core/`) across macOS and Windows. Only the installation differs by platform.

---

## 1. What it does

Feed a book or paper as PDF/TXT, and it produces **readable summary Wiki notes** saved into your Obsidian vault through these stages:

```
PDF/TXT  →  Text conversion  →  Chapter split  →  Translation (English docs only)  →  Summaries  →  Obsidian Wiki
```

- Works on **text-based PDFs** (with a text layer) or TXT — not raw scans.
- Translation, summarization and Wiki generation use **AI**: enter an API key, or enable a Claude/ChatGPT subscription CLI.
- Notes include the **author, a key summary, per-chapter overview, key quotes and key keywords** (with explanations); dense source texts are made readable by glossing terms in the original language and paraphrasing in plain language.

---

## 2. Installation

### macOS

1. Download `MyBookshelf-vX.Y.Z-mac.zip` from the [latest release](https://github.com/Brightinyou/my-bookshelf-for-mac/releases/latest) and unzip it.
2. Move `MyBookshelf.app` to your **Applications** folder.
3. For the first launch, **right-click → Open** (an expected warning for an unsigned personal build).
4. The first run auto-installs the required Python packages (a few minutes). After that, double-click the icon to open the native window.

> Requires Python 3.10+. If missing, install from [python.org](https://www.python.org/downloads/).

### Windows

1. Download `Setup.exe` from the [latest release](https://github.com/Brightinyou/my-bookshelf-for-pc/releases/latest).
2. If SmartScreen warns, choose **More info → Run** (expected for an unsigned personal build).
3. Pick the **install language (Korean/English)** — it becomes the app's default language.
4. Package download takes a few minutes; the app launches automatically, and afterwards opens from the **My Bookshelf** icon (desktop / Start menu).

> Requires Python 3.10+. If missing, `Setup.exe` offers to install it. The PDF text extractor (Poppler) is bundled — no separate install needed.

---

## 3. First-time setup — connect an AI

In the app's `⚙️ Settings` tab, set up **one of two** options. **You choose the AI model once in Settings** and every stage uses it.

- **AI subscription (CLI)** — use via subscription without an API key (recommended). Takes priority over API keys.
  - **Claude**: Claude (Pro/Max) subscribers. Install & log in to the `claude` CLI, then turn on the toggle.
  - **Codex**: ChatGPT (Plus/Pro) subscribers. Install & log in to the `codex` CLI, then turn on the toggle.
- **AI API keys** — enter a Gemini / OpenAI / Anthropic key directly.

After installing and logging in, restart the app so Settings detects it. Login is browser-based and only needed once.

Also confirm/choose the folder (vault) where Wiki notes are saved in `⚙️ Settings → Obsidian vault settings`.

---

## 4. The workflow in detail

Switch stages from the top menu. Every upload area accepts **file picker or drag & drop**. The "open folder" buttons are tucked into a small expander so the actual work area stands out.

### ① 📄 Text conversion
- Uploaded PDF/TXT files stack up in the **processing queue**.
- Select items and press **[Convert to text]** — extracts the text layer and saves TXT. The original PDF is kept.
- **[Delete]** removes mistakenly added files.
- **Fetch from a paper source**: pull a paper by URL, DOI or arXiv number (for login/paywalled pages, download the PDF yourself and upload it).

### ② 📂 Chapter split
- Splits a book TXT into **per-chapter files** under a per-book folder.
- **[Split]** — split into chapters. **[Move to next step]** — if no split is needed, send the whole document onward (EN → Translation, KO → Summaries).
- Short documents are handled separately under "Short documents".

### ③ 🌐 Translation
- Translates chapters into Korean, saved as `_ko.txt`.
- Uploaded TXT is added to the **translation queue**; translation starts only when you press **[▶ Start]**.
- **This stage is hidden in the English UI** — English users have no need to translate English into English (see §6).

### ④ 📝 Summaries
- Creates per-chapter summary notes (`_wiki.md`) — author, key summary, overview, key quotes, key keywords (with explanations).
- Select queued items and press **[▶ Start]**.

### ⑤ 📖 Wiki
- Merges summaries into a **hub note + per-chapter notes** in the Obsidian vault.
- Use **[Select all]/[Clear]** in the queue, then **[▶ Start]**.

---

## 5. Start · Stop · Resume

For the AI stages (Chapter split, Translation, Summaries, Wiki), pressing **[▶ Start]** switches to a processing view and **locks** other actions and tab navigation (so you can't accidentally leave a running job).

- The processing view shows progress and per-item results.
- **[■ Stop]** halts **after the current item finishes** and restores the full page.
- Remaining work stays in the queue — press **[▶ Start]** again to resume.
- Use **[🗑 Delete]** in the queue to drop wrongly added work.

---

## 6. Language and the translation stage

Switch Korean/English in `⚙️ Settings → Language`.

- **Switching to English hides the Translation stage** from the menu, navigation and pipeline. There is no point translating an English document back into English, so after chapter split, documents go straight to Summaries.
- Switching back to Korean brings the Translation stage back.

---

## 7. Data locations

Default data folders (folder names are Korean or English depending on the install language):

```
0_Inbox/            uploads/downloads waiting (pre-processing)
1_PDF_Originals/    original PDFs
2_Converted_TXT/    converted TXT (done/ = archived sources after split)
3_Chapters/<book>/  workspace holding chapters, translations (_ko), summaries (_wiki.md), overview
Failed/, Logs/      failed files, logs
```

Wiki notes are saved to a separate Obsidian vault (chosen in `⚙️ Settings`). Settings live in `~/.config/mybookshelf/config.json` (macOS/Linux) or the same path under your user profile.

---

## 8. Troubleshooting

- **"No AI available"** — enter an API key or enable a CLI subscription (Claude/Codex) in `⚙️ Settings`.
- **Old screen after an update** — fully quit the app and reopen it (a server may still be running).
- **Scanned PDFs** — OCR scans into a text PDF/TXT first, then feed them in.
- (Windows) For install/launch errors, check `install.log` / `launch-error.log` in the install folder.

---

## 9. Copyright and disclaimer

**My Bookshelf** — © 2026 the author. Provided for personal, non-commercial research use.

**About the program**
- Copyright in this program belongs to the author. You may use and copy it for personal and academic purposes, but you may not resell or commercially distribute it without the author's written consent.
- The program is provided "as-is", with no warranty of fitness for a particular purpose or integrity. The author is not liable for any data loss or damage from its use.

**About your documents and generated output**
- You are responsible for confirming the source document's copyright, translation, summary and redistribution rights. This program does not automatically judge legal, publishing or academic-submission requirements.
- Enabling an AI API or CLI tool sends part or all of your document to an external AI service. Do not input sensitive data, unpublished manuscripts, or material whose distribution rights are unclear.
- Accuracy and completeness of generated translations, summaries and Wiki notes are not guaranteed. Always compare against the source before publishing, submitting, citing or distributing.

---

## For developers

```
core/                app core
  pipeline_app.py    Streamlit UI (all stages)
  services/          processing logic (convert/translate/chapters/wiki/i18n …)
  chapter_wiki.py    chapter split + summary generation (multi-provider AI)
  llm_providers.py   AI provider abstraction (Gemini/OpenAI/Anthropic/Claude CLI/Codex CLI)
  .streamlit/        config.toml (light theme, developer toolbar disabled)
dev/                 build scripts (build_mac_app.sh, bump_version.py …)
```

- macOS build: `dev/build_mac_app.sh` → `dist/mac/MyBookshelf.app`
- Run from source: each platform's `start` script, or `streamlit run core/pipeline_app.py`
