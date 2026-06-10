"""llm_providers.py — 멀티 공급자 LLM 통일 호출 + 키 관리 (2026-06-09)

OpenAI(GPT) / Google(Gemini) / Anthropic(Claude API) + Claude CLI(구독).
키 우선순위: 환경변수 → ~/.config/mybookshelf/keys.json → (gemini는 ~/.config/gemini_wiki.key).
키는 이 컴퓨터 로컬에만 저장하며 저장소/외부로 전송하지 않는다.
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
GEMINI_WIKI_KEY = Path.home() / ".config" / "gemini_wiki.key"  # 위키 생성기 호환

# 공급자 레지스트리 — provider 키: {label, models[], env(환경변수명), hint}
PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "env": "GEMINI_API_KEY",
        "hint": "AIza… 또는 AQ.…",
    },
    "openai": {
        "label": "OpenAI GPT",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "env": "OPENAI_API_KEY",
        "hint": "sk-…",
    },
    "anthropic": {
        "label": "Anthropic Claude (API)",
        "models": ["claude-sonnet-4-6", "claude-haiku-4-5"],
        "env": "ANTHROPIC_API_KEY",
        "hint": "sk-ant-…",
    },
}


def _load_all() -> dict:
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8")) if KEYS_FILE.exists() else {}
    except Exception:
        return {}


def get_key(provider: str) -> str:
    """우선순위: 설정(keys.json) → (gemini 한정) gemini_wiki.key → 환경변수.
    사용자가 관리하는 명시적 키 파일이 launch 환경변수(만료/구버전 가능)보다 우선한다."""
    v = (_load_all().get(provider) or "").strip()
    if v:
        return v
    if provider == "gemini" and GEMINI_WIKI_KEY.exists():
        fk = GEMINI_WIKI_KEY.read_text(encoding="utf-8").strip()
        if fk:
            return fk
    info = PROVIDERS.get(provider, {})
    env = info.get("env")
    if env and os.environ.get(env, "").strip():
        return os.environ[env].strip()
    return ""


def save_key(provider: str, key: str) -> None:
    """keys.json에 저장(빈 값이면 삭제). 파일 권한 0600.
    gemini는 위키 생성기 호환을 위해 gemini_wiki.key에도 동기화."""
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
    # gemini → 위키 생성기(gemini_wiki.py)가 읽는 파일에도 반영
    if provider == "gemini":
        try:
            if key:
                GEMINI_WIKI_KEY.write_text(key, encoding="utf-8")
                os.chmod(GEMINI_WIKI_KEY, 0o600)
            elif GEMINI_WIKI_KEY.exists():
                GEMINI_WIKI_KEY.unlink()
        except Exception:
            pass


def has_key(provider: str) -> bool:
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
    return bool(claude_cli_path())


# ── 통일 호출: text-in → text-out ──
def complete(provider: str, model: str, system: str, prompt: str,
             max_tokens: int = 8192, api_key: str | None = None) -> str:
    """선택 공급자/모델로 1회 완성. 키 없거나 호출 실패하면 예외를 던진다."""
    if provider == "claude_cli":
        return _claude_cli(model, system, prompt)

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


def _strip_fence(t: str) -> str:
    t = (t or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(json)?|```$", "", t.strip()).strip()
    return t


def complete_json(provider: str, model: str, system: str, prompt: str,
                  max_tokens: int = 16384, api_key: str | None = None, retries: int = 5) -> dict:
    """JSON 출력 통일(공급자별 JSON 모드). 위키 생성용. 실패 시 재시도(429는 65초)."""
    key = (api_key or get_key(provider)).strip()
    if not key:
        raise RuntimeError(f"{provider} API 키 없음")
    last = None
    for attempt in range(retries):
        try:
            if provider == "gemini":
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
