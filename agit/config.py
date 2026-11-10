"""Configuration: provider presets, env resolution, project config file.

Nothing is hardcoded about any single vendor. agit talks to any
OpenAI-compatible /chat/completions endpoint, so it works with BharatCode,
a local Ollama, or any free tier you already have.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

AGIT_DIR = ".agit"
CONFIG_FILE = "config.json"

# Provider presets. `url_envs`/`key_envs` are consulted in order; the first
# env var that is set wins. `free` documents how to get access without paying,
# which `agit doctor` prints when nothing is configured.
PRESETS: Dict[str, Dict[str, Any]] = {
    "bharatcode": {
        "base_url": "",
        "url_envs": ["BHARATCODE_BASE_URL", "AGIT_BASE_URL"],
        "key_envs": ["BHARATCODE_API_KEY", "AGIT_API_KEY"],
        "model": "qwen-3.8-27b",
        "model_envs": ["BHARATCODE_MODEL", "AGIT_MODEL"],
        "free": "Free student access / hackathon key from your BharatCode account page.",
        "hint": "Set BHARATCODE_BASE_URL to the API base from your BharatCode API page "
                "(the URL ending in /v1) and BHARATCODE_API_KEY to your key.",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "url_envs": ["OLLAMA_BASE_URL", "AGIT_BASE_URL"],
        "key_envs": [],
        "model": "qwen2.5-coder:7b",
        "model_envs": ["AGIT_MODEL"],
        "free": "Fully free and offline: `ollama pull qwen2.5-coder:7b`.",
        "hint": "Start ollama, then: agit run --provider ollama '<task>'",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "url_envs": ["AGIT_BASE_URL"],
        "key_envs": ["GROQ_API_KEY", "AGIT_API_KEY"],
        "model": "openai/gpt-oss-120b",
        "model_envs": ["AGIT_MODEL"],
        "free": "Groq has a free plan; limits are shown in your console settings.",
        "hint": "export GROQ_API_KEY=...",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "url_envs": ["AGIT_BASE_URL"],
        "key_envs": ["OPENROUTER_API_KEY", "AGIT_API_KEY"],
        "model": "",
        "model_envs": ["AGIT_MODEL"],
        "free": "OpenRouter lists models at $0; pick one ending in ':free'.",
        "hint": "export OPENROUTER_API_KEY=... AGIT_MODEL=<model>:free",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "url_envs": ["AGIT_BASE_URL"],
        "key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "AGIT_API_KEY"],
        "model": "gemini-3.8-flash",
        "model_envs": ["AGIT_MODEL"],
        "free": "Google AI Studio: Flash-tier models are free of charge.",
        "hint": "export GEMINI_API_KEY=...",
    },
    "openai-compatible": {
        "base_url": "",
        "url_envs": ["AGIT_BASE_URL", "OPENAI_BASE_URL"],
        "key_envs": ["AGIT_API_KEY", "OPENAI_API_KEY"],
        "model": "",
        "model_envs": ["AGIT_MODEL"],
        "free": "Anything that speaks POST {base}/chat/completions.",
        "hint": "export AGIT_BASE_URL=... AGIT_API_KEY=... AGIT_MODEL=...",
    },
    "script": {
        "base_url": "",
        "url_envs": [],
        "key_envs": [],
        "model": "agit-script/deterministic",
        "model_envs": [],
        "free": "No network, no key. A deterministic scripted planner used for "
                "the offline demo, the test suite and CI.",
        "hint": "agit demo",
    },
}

#: Providers that never touch the network.
OFFLINE_PROVIDERS = ("script",)


def _first_env(names: List[str]) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


@dataclass
class Config:
    provider: str = "bharatcode"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.0
    max_steps: int = 24
    budget_tokens: int = 250_000
    request_timeout: int = 180
    native_tools: bool = True          # use OpenAI tool-calling; falls back to JSON protocol
    allow_commands: bool = False       # run_command without asking
    verify: str = ""                   # command that must pass for the run to be VERIFIED
    # live  : always ask the model, record every answer (default)
    # reuse : answer from the cassette when the exact prompt was seen before
    # replay: cassette only, never touch the network
    cassette_mode: str = "live"
    snapshot_max_bytes: int = 512 * 1024

    def redacted(self) -> Dict[str, Any]:
        d = asdict(self)
        d["api_key"] = ("set (%d chars)" % len(self.api_key)) if self.api_key else "unset"
        return d

    @property
    def offline(self) -> bool:
        return self.provider in OFFLINE_PROVIDERS

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def project_config_path(root: str) -> str:
    return os.path.join(root, AGIT_DIR, CONFIG_FILE)


def load_project_config(root: str) -> Dict[str, Any]:
    path = project_config_path(root)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_project_config(root: str, data: Dict[str, Any]) -> str:
    path = project_config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def detect_provider() -> str:
    """Pick a provider when the user has not named one.

    Order: explicit env -> a provider whose key is present -> local ollama
    -> the offline scripted planner (so `agit` is never dead on arrival).
    """
    env = os.environ.get("AGIT_PROVIDER", "").strip()
    if env:
        return env
    for name in ("bharatcode", "groq", "openrouter", "gemini"):
        preset = PRESETS[name]
        if _first_env(preset["key_envs"]):
            if name != "bharatcode" or _first_env(preset["url_envs"]):
                return name
    if _first_env(PRESETS["openai-compatible"]["url_envs"]):
        return "openai-compatible"
    if os.environ.get("OLLAMA_HOST") or os.environ.get("AGIT_ASSUME_OLLAMA"):
        return "ollama"
    return "script"


def resolve(root: str = ".", overrides: Optional[Dict[str, Any]] = None) -> Config:
    """Build a Config from defaults <- env <- project config <- CLI overrides."""
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    proj = load_project_config(root)

    provider = (
        overrides.get("provider")
        or proj.get("provider")
        or detect_provider()
    )
    preset = PRESETS.get(provider)
    if preset is None:
        known = ", ".join(sorted(PRESETS))
        raise ConfigError("unknown provider %r (known: %s)" % (provider, known))

    cfg = Config(provider=provider)
    cfg.base_url = _first_env(preset["url_envs"]) or preset["base_url"]
    cfg.model = _first_env(preset["model_envs"]) or preset["model"]
    cfg.api_key = _first_env(preset["key_envs"])

    for key in ("base_url", "model", "temperature", "max_steps", "budget_tokens",
                "request_timeout", "native_tools", "allow_commands", "verify",
                "cassette_mode", "snapshot_max_bytes"):
        if key in proj:
            setattr(cfg, key, proj[key])
    if os.environ.get("AGIT_MAX_STEPS", "").isdigit():
        cfg.max_steps = int(os.environ["AGIT_MAX_STEPS"])
    if os.environ.get("AGIT_NO_NATIVE_TOOLS"):
        cfg.native_tools = False
    if os.environ.get("AGIT_YES"):
        cfg.allow_commands = True

    for key, value in overrides.items():
        if key == "provider":
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    cfg.temperature = float(cfg.temperature)
    cfg.max_steps = max(1, int(cfg.max_steps))
    return cfg


class ConfigError(Exception):
    pass


def check(cfg: Config) -> List[str]:
    """Return human-readable problems that would stop a live run."""
    problems: List[str] = []
    if cfg.offline:
        return problems
    preset = PRESETS[cfg.provider]
    if not cfg.base_url:
        problems.append(
            "no API base URL for provider '%s'. %s" % (cfg.provider, preset["hint"])
        )
    if preset["key_envs"] and not cfg.api_key:
        problems.append(
            "no API key for provider '%s'. Set %s" % (cfg.provider, " or ".join(preset["key_envs"]))
        )
    if not cfg.model:
        problems.append("no model set. Export AGIT_MODEL or pass --model.")
    return problems
