"""llm_providers.py — 멀티 공급자 LLM 통일 호출 + 키 관리 (2026-06-15)

OpenAI(GPT) / Google(Gemini) / Anthropic(Claude API) + Claude CLI(구독) + Codex CLI(구독).
키는 앱 설정 파일에 저장한 값을 우선 사용하고, 없으면 이 컴퓨터의 환경변수에서 감지한다.
저장 키는 이 컴퓨터 로컬에만 저장하며 저장소/외부로 전송하지 않는다.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "mybookshelf"
KEYS_FILE = CONFIG_DIR / "keys.json"

ENV_KEY_NAMES: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
}

# 공급자 레지스트리 — provider 키: {label, models[], hint}
PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "hint": "Gemini API key",
    },
    "openai": {
        "label": "OpenAI GPT",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "hint": "sk-…",
    },
    "anthropic": {
        "label": "Anthropic Claude (API)",
        "models": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "hint": "sk-ant-…",
    },
    "claude_cli": {
        "label": "Claude CLI (구독)",
        "models": ["claude-sonnet-4-6", "claude-opus-4-8"],
        "hint": "",
    },
    "codex_cli": {
        "label": "Codex CLI (ChatGPT)",
        "models": ["default"],  # ChatGPT 계정은 모델 지정 불가(o3/o4-mini 400오류) → 기본 모델 사용
        "hint": "",
    },
}

# 공급자별 안전 입력 한도 (chars). Gemini=1M 토큰, Claude/GPT=200k/128k 토큰 기준.
# 한국어 기준 roughly 1char≈1token, 영어는 1char≈0.25token — 한국어 기준으로 보수적으로 설정.
MAX_INPUT_CHARS: dict[str, int] = {
    "gemini":    1_900_000,   # Gemini 2.5: 1M 토큰
    "openai":      400_000,   # GPT-4o: 128k 토큰
    "anthropic":   140_000,   # Claude: 200k 토큰 — 출력 여유 60k 확보
    "claude_cli":  140_000,   # Claude CLI: 구독 = API 동일 한도
    "codex_cli":   400_000,   # Codex CLI: OpenAI 모델 기반 (o3/gpt-4o)
}


def _no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _load_all() -> dict:
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8")) if KEYS_FILE.exists() else {}
    except Exception:
        return {}


def saved_key(provider: str) -> str:
    """Return a key explicitly saved in the app settings screen."""
    all_keys = _load_all()
    return (all_keys.get(provider) or "").strip()


def detected_key(provider: str) -> str:
    """Return a key detected from local environment variables."""
    for name in ENV_KEY_NAMES.get(provider, ()):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def get_key(provider: str) -> str:
    """Return the configured key, preferring app settings over detected env keys."""
    return saved_key(provider) or detected_key(provider)


def key_source(provider: str) -> str:
    if saved_key(provider):
        return "saved"
    if detected_key(provider):
        return "detected"
    return ""


def save_key(provider: str, key: str) -> None:
    """Save keys to keys.json. Empty values clear the saved key; env fallback still works."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _load_all()
    key = (key or "").strip()
    if key:
        data[provider] = key
    else:
        data.pop(provider, None)
    KEYS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


def has_key(provider: str) -> bool:
    if provider == "claude_cli":
        return claude_cli_available()
    if provider == "codex_cli":
        return codex_cli_available()
    return bool(get_key(provider))


def masked(provider: str) -> str:
    k = get_key(provider)
    if not k:
        return ""
    return f"{k[:4]}…{k[-4:]}" if len(k) > 10 else "•" * len(k)


# ── 위키 생성 모델 설정 (provider+model) ──
def wiki_provider_model() -> tuple[str, str]:
    """위키 생성에 쓸 (provider, model). 설정 없으면 gemini-2.5-flash."""
    d = _load_all()
    prov = d.get("wiki_provider") or "gemini"
    if prov not in PROVIDERS:
        prov = "gemini"
    model = d.get("wiki_model") or PROVIDERS[prov]["models"][0]
    return prov, model

def set_wiki_model(provider: str, model: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    d = _load_all()
    d["wiki_provider"], d["wiki_model"] = provider, model
    KEYS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


# ── UI 선호 설정 (번역 토글 등 — 재시작해도 유지, 2026-06-11) ──
def get_pref(key: str, default=None):
    return _load_all().get("pref_" + key, default)


def set_pref(key: str, value) -> None:
    d = _load_all()
    if d.get("pref_" + key) == value:
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    d["pref_" + key] = value
    KEYS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


# ── Claude CLI (구독) ──
def claude_cli_path() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    home = Path.home()
    for cand in (
        Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude"),
        home / ".local" / "bin" / "claude",        # 네이티브 설치(맥·리눅스)
        home / ".local" / "bin" / "claude.exe",    # 네이티브 설치(윈도우)
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",  # npm 전역(윈도우)
    ):
        if cand.exists():
            return str(cand)
    return None


def claude_cli_available() -> bool:
    return bool(get_pref("use_claude_cli", False)) and bool(claude_cli_path())


def claude_cli_installed() -> bool:
    return bool(claude_cli_path())


def set_claude_cli_enabled(enabled: bool) -> None:
    set_pref("use_claude_cli", bool(enabled))


# ── Codex CLI (OpenAI 구독) ──
def codex_cli_path() -> str | None:
    p = shutil.which("codex")
    if p:
        return p
    home = Path.home()
    for cand in (
        Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"),
        home / ".local" / "bin" / "codex",
        home / ".local" / "bin" / "codex.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd",
    ):
        if cand.exists():
            return str(cand)
    return None


def codex_cli_available() -> bool:
    return bool(get_pref("use_codex_cli", False)) and bool(codex_cli_path())


def codex_cli_installed() -> bool:
    return bool(codex_cli_path())


def set_codex_cli_enabled(enabled: bool) -> None:
    set_pref("use_codex_cli", bool(enabled))


# ── 통일 호출: text-in → text-out ──
def complete(provider: str, model: str, system: str, prompt: str,
             max_tokens: int = 8192, api_key: str | None = None) -> str:
    """선택 공급자/모델로 1회 완성. 키 없거나 호출 실패하면 예외를 던진다."""
    if provider == "claude_cli":
        return _claude_cli(model, system, prompt)
    if provider == "codex_cli":
        return _codex_cli(model, system, prompt)

    key = (api_key or get_key(provider)).strip()
    if not key:
        raise RuntimeError(f"{provider} API 키 없음")

    if provider == "gemini":
        from google import genai
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=model, contents=[system, prompt])
        return (resp.text or "").strip()

    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=model, system=system, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    raise RuntimeError(f"알 수 없는 공급자: {provider}")


def _claude_cli(model: str, system: str, prompt: str) -> str:
    cli = claude_cli_path()
    if not cli:
        raise RuntimeError("claude CLI 없음")
    r = subprocess.run(
        [cli, "-p", prompt, "--model", model,
         "--system-prompt", system, "--output-format", "text"],
        capture_output=True, text=True, timeout=600, cwd=tempfile.gettempdir(),
        encoding="utf-8", errors="replace",   # 윈도우 cp949가 한글 UTF-8 출력 못 읽음 (2026-06-11)
        stdin=subprocess.DEVNULL,             # 미지정 시 CLI가 stdin 3초 대기 — 단락마다 지연
        **_no_window_kwargs(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI exit {r.returncode}: {(r.stderr or '')[:200]}")
    out = (r.stdout or "").strip()
    # ~/.claude 자동 메모리 hook이 출력 끝에 붙는 경우 제거
    for marker in ("\n메모리 저장:", "\n저장할 새 메모리 없음"):
        idx = out.rfind(marker)
        if idx != -1:
            out = out[:idx].rstrip()
    return out


def _codex_cli(model: str, system: str, prompt: str) -> str:
    cli = codex_cli_path()
    if not cli:
        raise RuntimeError("codex CLI 없음")
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    out_file = Path(tempfile.gettempdir()) / f"codex_out_{os.getpid()}.txt"
    base_args = [cli, "exec", "--skip-git-repo-check", "--ephemeral",
                 "--dangerously-bypass-approvals-and-sandbox",
                 "-o", str(out_file)]
    # ChatGPT 계정은 모델 명시 시 400 오류 → default 또는 불지원 오류면 모델 없이 실행
    if model in ("default", ""):
        attempts = [[full_prompt]]
    else:
        attempts = [["-m", model, full_prompt], [full_prompt]]
    try:
        last_err = None
        for extra in attempts:
            r = subprocess.run(
                base_args + extra,
                capture_output=True, text=True, timeout=600,
                cwd=tempfile.gettempdir(), encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,
                **_no_window_kwargs(),
            )
            if r.returncode == 0:
                if out_file.exists():
                    return out_file.read_text(encoding="utf-8").strip()
                return (r.stdout or "").strip()
            err = (r.stderr or "")
            if "not supported" in err or "invalid_request" in err:
                last_err = err
                out_file.unlink(missing_ok=True)
                continue  # 모델 없이 재시도
            raise RuntimeError(f"codex CLI exit {r.returncode}: {err[:300]}")
        raise RuntimeError(f"codex CLI 실패: {(last_err or '')[:300]}")
    finally:
        out_file.unlink(missing_ok=True)


def _strip_fence(t: str) -> str:
    t = (t or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(json)?|```$", "", t.strip()).strip()
    return t


def complete_json(provider: str, model: str, system: str, prompt: str,
                  max_tokens: int = 16384, api_key: str | None = None, retries: int = 5) -> dict:
    """JSON 출력 통일(공급자별 JSON 모드). 위키 생성용. 실패 시 재시도(429는 65초)."""
    if provider in ("claude_cli", "codex_cli"):
        # API 키 불필요 — CLI 구독 사용. 재시도 루프에서 처리.
        key = ""
    else:
        key = (api_key or get_key(provider)).strip()
        if not key:
            raise RuntimeError(f"{provider} API 키 없음")
    last = None
    for attempt in range(retries):
        try:
            if provider == "claude_cli":
                txt = _claude_cli(model, system or "Output only one valid JSON object.",
                                  prompt + "\n\n반드시 유효한 JSON 객체 하나만 출력하라.")
            elif provider == "codex_cli":
                txt = _codex_cli(model, system or "Output only one valid JSON object.",
                                 prompt + "\n\n반드시 유효한 JSON 객체 하나만 출력하라.")
            elif provider == "gemini":
                from google import genai
                client = genai.Client(api_key=key)
                contents = [system, prompt] if system else prompt
                resp = client.models.generate_content(
                    model=model, contents=contents,
                    config={"temperature": 0.3, "response_mime_type": "application/json",
                            "max_output_tokens": max_tokens})
                txt = resp.text or ""
            elif provider == "openai":
                from openai import OpenAI
                client = OpenAI(api_key=key)
                resp = client.chat.completions.create(
                    model=model, max_tokens=max_tokens, temperature=0.3,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system or "Output only valid JSON."},
                              {"role": "user", "content": prompt}])
                txt = resp.choices[0].message.content or ""
            elif provider == "anthropic":
                import anthropic
                client = anthropic.Anthropic(api_key=key)
                resp = client.messages.create(
                    model=model, system=system or "Output only one valid JSON object.",
                    max_tokens=max_tokens, temperature=0.3,
                    messages=[{"role": "user", "content": prompt + "\n\n반드시 유효한 JSON 객체 하나만 출력하라."}])
                txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            else:
                raise RuntimeError(f"JSON 미지원 공급자: {provider}")
            return json.loads(_strip_fence(txt))
        except Exception as e:
            last = e
            if attempt >= retries - 1:
                raise
            m = str(e).lower()
            is_429 = "429" in m or "resource_exhausted" in m or "rate_limit" in m or "overloaded" in m
            time.sleep(65 if is_429 else 4)
    raise last


