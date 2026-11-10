"""Model access: one OpenAI-compatible client, plus record/replay cassettes.

Cassettes are what make an agent run reproducible. Every request is hashed
with its full message list, so `agit replay` can re-drive an identical run
with no network and no spend, and `agit replay --live` can re-ask the model
and report exactly where this run diverges from the recorded one.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import Config

USER_AGENT = "agit/1.0 (+https://github.com/VamsiKrishnaThota03/agit)"

#: Status codes worth retrying, and how patient to be about it. Free tiers are
#: aggressively rate limited and usually tell you exactly how long to wait, so
#: honouring that is the difference between a run finishing and a run dying.
RETRYABLE = (408, 409, 425, 429, 500, 502, 503, 504)
MAX_ATTEMPTS = 6
MAX_TOTAL_WAIT = 120.0          # seconds spent waiting, across one request
MAX_SINGLE_WAIT = 45.0

#: Providers phrase it differently; all of these appear in the wild.
_RETRY_HINTS = (
    r"try again in ([0-9.]+)\s*s",
    r"retry after ([0-9.]+)\s*s",
    r"please wait ([0-9.]+)\s*s",
    r"in ([0-9.]+)\s*seconds",
)


#: Things that change between two identical runs and must not affect the
#: cassette key: elapsed times, pids, memory addresses, clock times, temp dirs.
VOLATILE = [
    (r"\b\d+\.\d{1,6}s\b", "<elapsed>"),
    (r"\b\d{1,2}:\d{2}:\d{2}(\.\d+)?\b", "<time>"),
    (r"0x[0-9a-fA-F]{6,}", "<addr>"),
    (r"\bpid[= ]\d+\b", "pid=<pid>"),
    (r"/(tmp|var/folders)/[\w./-]+", "<tmp>"),
    (r"\b\d{10,13}\b", "<epoch>"),
]
_VOLATILE_RX = None


def mask_volatile(text: str) -> str:
    """Normalise run-to-run noise so replay compares like with like."""
    global _VOLATILE_RX
    if _VOLATILE_RX is None:
        import re
        _VOLATILE_RX = [(re.compile(p), r) for p, r in VOLATILE]
    for rx, replacement in _VOLATILE_RX:
        text = rx.sub(replacement, text)
    return text


def retry_delay(headers: Any, body: str, attempt: int) -> float:
    """How long the provider wants us to wait, or a backoff if it did not say.

    Returned uncapped on purpose: the caller refuses outright when a provider
    asks for longer than agit will hold a request, because sleeping 45s when
    the answer is "come back in an hour" only wastes 45 seconds.
    """
    import re

    if headers is not None:
        for name in ("Retry-After", "retry-after", "X-RateLimit-Reset-Tokens",
                     "x-ratelimit-reset-tokens"):
            raw = None
            try:
                raw = headers.get(name)
            except AttributeError:
                raw = None
            if raw:
                match = re.match(r"^\s*([0-9.]+)\s*(m?s)?\s*$", str(raw))
                if match:
                    value = float(match.group(1))
                    if match.group(2) == "ms":
                        value /= 1000.0
                    return max(0.5, value + 0.5)
    low = (body or "").lower()
    for pattern in _RETRY_HINTS:
        match = re.search(pattern, low)
        if match:
            try:
                return max(0.5, float(match.group(1)) + 0.5)
            except ValueError:
                pass
    return min(MAX_SINGLE_WAIT, 1.5 ** attempt + 0.5)


class ModelError(RuntimeError):
    pass


class ReplayMiss(ModelError):
    pass


class ToolCallRejected(ModelError):
    """The provider refused the model's tool call, not agit's request.

    Groq and others validate tool calls server-side and answer HTTP 400 with
    code `tool_use_failed` when the model invents a malformed call. That is a
    model mistake, recoverable by telling the model what it got wrong -- so it
    must not kill the run the way a bad API key would.
    """

    def __init__(self, detail: str, generation: str = ""):
        super().__init__("the model emitted a tool call the provider rejected: %s"
                         % detail[:400])
        self.detail = detail
        self.generation = generation


#: Provider markers for "your model botched the tool call".
TOOL_REJECTION_MARKERS = (
    "tool_use_failed",
    "tool call validation failed",
    "invalid tool call",
    "did not match schema",
    "failed_generation",
)


def _is_tool_rejection(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in TOOL_REJECTION_MARKERS)


@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    id: str = "call_0"

    def signature(self) -> str:
        return "%s(%s)" % (self.name, json.dumps(self.args, sort_keys=True)[:400])


@dataclass
class Reply:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    model: str = ""
    duration_ms: int = 0

    @property
    def call(self) -> Optional[ToolCall]:
        return self.tool_calls[0] if self.tool_calls else None


# -- cassette -------------------------------------------------------------

class Cassette:
    """A JSON file mapping request-hash -> raw response."""

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        self.dirty = False
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self.data = loaded.get("entries", loaded)
            except (OSError, ValueError):
                self.data = {}

    def save(self) -> None:
        if not self.dirty:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {"version": 1, "entries": self.data}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False

    @staticmethod
    def key(payload: Dict[str, Any]) -> str:
        trimmed = {
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "messages": payload.get("messages"),
            "tools": payload.get("tools"),
        }
        blob = json.dumps(trimmed, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(mask_volatile(blob).encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self.data.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry.get("response") if isinstance(entry, dict) and "response" in entry else entry

    def put(self, key: str, response: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> None:
        self.data[key] = {"response": response, "meta": meta or {}}
        self.dirty = True

    def __len__(self) -> int:
        return len(self.data)


# -- client ---------------------------------------------------------------

class ModelClient:
    def __init__(
        self,
        cfg: Config,
        cassette: Optional[Cassette] = None,
        script: Optional[Any] = None,
        on_retry: Optional[Any] = None,
    ):
        self.cfg = cfg
        self.cassette = cassette
        self.script = script
        self.on_retry = on_retry        # called with a human-readable reason
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.network_calls = 0

    # -- public -----------------------------------------------------------
    def complete(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Reply:
        payload: Dict[str, Any] = {
            "model": self.cfg.model or "unset",
            "messages": messages,
            "temperature": self.cfg.temperature,
        }
        if tools and self.cfg.native_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        key = Cassette.key(payload)
        started = time.time()

        if self.cfg.cassette_mode == "replay":
            raw = self.cassette.get(key) if self.cassette else None
            if raw is None:
                raise ReplayMiss(
                    "no recorded response for this request. The run diverged from the "
                    "cassette (step %d). Use --live to re-ask the model." % (self.calls + 1)
                )
            reply = self._to_reply(raw, cached=True)
        else:
            raw = None
            if self.cfg.cassette_mode == "reuse" and self.cassette is not None:
                raw = self.cassette.get(key)
            if raw is not None:
                reply = self._to_reply(raw, cached=True)
            else:
                raw = self._dispatch(payload)
                if self.cassette is not None:
                    self.cassette.put(key, raw, {"model": payload["model"], "at": int(time.time())})
                reply = self._to_reply(raw, cached=False)

        reply.duration_ms = int((time.time() - started) * 1000)
        self.calls += 1
        self.tokens_in += reply.tokens_in
        self.tokens_out += reply.tokens_out
        return reply

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    # -- transport --------------------------------------------------------
    def _dispatch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.script is not None:
            return self.script.respond(payload)
        if self.cfg.offline:
            raise ModelError("provider %r is offline but no script is loaded" % self.cfg.provider)
        self.network_calls += 1
        return self._http(payload)

    def _http(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.cfg.base_url:
            raise ModelError("no API base URL configured (see `agit doctor`)")
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.cfg.api_key:
            headers["Authorization"] = "Bearer " + self.cfg.api_key
        last_error = ""
        waited = 0.0
        for attempt in range(MAX_ATTEMPTS):
            final = attempt == MAX_ATTEMPTS - 1
            request = urllib.request.Request(
                self.cfg.endpoint(), data=body, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=self.cfg.request_timeout) as resp:
                    text = resp.read().decode("utf-8", "replace")
                return json.loads(text)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:600]
                last_error = "HTTP %s: %s" % (exc.code, detail)
                if exc.code == 400 and _is_tool_rejection(detail):
                    raise ToolCallRejected(detail, _failed_generation(detail))
                if exc.code not in RETRYABLE or final:
                    raise ModelError(_explain_http(exc.code, detail, self.cfg))
                delay = retry_delay(getattr(exc, "headers", None), detail, attempt)
                if delay > MAX_SINGLE_WAIT or waited + delay > MAX_TOTAL_WAIT:
                    raise ModelError(_explain_http(exc.code, detail, self.cfg))
                self._notify_retry(exc.code, delay, detail)
                waited += delay
                time.sleep(delay)
            except urllib.error.URLError as exc:
                last_error = str(exc.reason)
                if final:
                    raise ModelError(
                        "cannot reach %s (%s). Check the base URL, your network, or run "
                        "`agit doctor`." % (self.cfg.endpoint(), last_error)
                    )
                delay = min(6.0, 1.5 ** attempt)
                self._notify_retry(0, delay, last_error)
                waited += delay
                time.sleep(delay)
            except ValueError as exc:
                raise ModelError("provider returned a non-JSON body: %s" % exc)
        raise ModelError("request failed after retries: %s" % last_error)

    def _notify_retry(self, code: int, delay: float, detail: str) -> None:
        if self.on_retry is None:
            return
        reason = "rate limited" if code == 429 else (
            "HTTP %d" % code if code else "network error")
        self.on_retry("%s, waiting %.1fs before retry" % (reason, delay))

    # -- parsing ----------------------------------------------------------
    def _to_reply(self, raw: Dict[str, Any], cached: bool) -> Reply:
        if not isinstance(raw, dict):
            raise ModelError("unexpected response type %s" % type(raw).__name__)
        if "error" in raw and "choices" not in raw:
            raise ModelError("provider error: %s" % json.dumps(raw["error"])[:400])
        choices = raw.get("choices") or []
        if not choices:
            raise ModelError("provider returned no choices: %s" % json.dumps(raw)[:300])
        message = choices[0].get("message") or {}
        text = (message.get("content") or "")
        if isinstance(text, list):  # some providers return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        text = text.strip()

        tool_calls: List[ToolCall] = []
        for i, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            args = fn.get("arguments")
            tool_calls.append(ToolCall(
                name=fn.get("name", ""),
                args=_coerce_args(args),
                id=call.get("id") or ("call_%d" % i),
            ))
        if not tool_calls and text:
            rationale, parsed = parse_text_action(text)
            if parsed is not None:
                tool_calls = [parsed]
                text = rationale or text

        usage = raw.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        if not tokens_in:
            tokens_in = _estimate(raw.get("_agit_prompt_chars", 0))
        if not tokens_out:
            tokens_out = _estimate(len(text) + sum(len(c.signature()) for c in tool_calls))

        return Reply(
            text=text,
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw=raw,
            cached=cached,
            model=raw.get("model") or self.cfg.model,
        )


def _estimate(chars: int) -> int:
    return max(0, int(chars / 4))


def _coerce_args(args: Any) -> Dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            repaired = _extract_json(args)
            if repaired is not None:
                return repaired
            return {"_raw": args}
    return {}


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first balanced JSON object out of free-form text."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        parsed = json.loads(chunk)
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


ACTION_KEYS = ("tool", "action", "name", "function", "tool_name")
ARG_KEYS = ("args", "arguments", "parameters", "params", "input", "tool_input")
WHY_KEYS = ("rationale", "thought", "reasoning", "why", "because", "plan")


def parse_text_action(text: str) -> Tuple[str, Optional[ToolCall]]:
    """Fallback protocol for models without native tool calling."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        for part in parts:
            if part.strip().startswith("json"):
                cleaned = part.strip()[4:]
                break
        else:
            cleaned = parts[1] if len(parts) > 1 else cleaned
    obj = _extract_json(cleaned)
    if obj is None:
        return text, None
    name = ""
    for key in ACTION_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value:
            name = value
            break
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            name = value["name"]
            obj = {**obj, "args": value.get("arguments", value.get("args", {}))}
            break
    if not name:
        return text, None
    args: Dict[str, Any] = {}
    for key in ARG_KEYS:
        value = obj.get(key)
        if isinstance(value, dict):
            args = value
            break
        if isinstance(value, str):
            parsed = _extract_json(value)
            args = parsed if parsed else {"value": value}
            break
    if not args:
        args = {k: v for k, v in obj.items()
                if k not in ACTION_KEYS + ARG_KEYS + WHY_KEYS}
    why = ""
    for key in WHY_KEYS:
        if isinstance(obj.get(key), str):
            why = obj[key]
            break
    return why, ToolCall(name=name, args=args)


def _failed_generation(detail: str) -> str:
    """What the model actually wrote, when the provider echoes it back."""
    try:
        payload = json.loads(detail)
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("failed_generation") or "")[:1500]
    return ""


def _explain_http(code: int, detail: str, cfg: Config) -> str:
    if code in (401, 403):
        return ("provider rejected the credentials (HTTP %d). Check your API key for "
                "provider '%s'. %s" % (code, cfg.provider, detail))
    if code == 404:
        return ("HTTP 404 from %s. The base URL or model name is probably wrong: base_url=%r "
                "model=%r. %s" % (cfg.endpoint(), cfg.base_url, cfg.model, detail))
    if code == 429:
        return ("rate limited by the provider (HTTP 429), and the wait it asked for is "
                "longer than agit will hold a single request. Try a smaller model, fewer "
                "steps, or a paid tier. %s" % detail)
    if code == 400:
        if _is_tool_rejection(detail):
            return ("the model emitted a malformed tool call and the provider rejected it. "
                    "agit normally hands this back to the model to retry. %s" % detail)
        return ("provider rejected the request (HTTP 400). If this model has no tool-calling "
                "support, retry with --no-native-tools. %s" % detail)
    return "HTTP %d from provider: %s" % (code, detail)


def wrap_response(content: str = "", tool_call: Optional[ToolCall] = None,
                  model: str = "agit-script/deterministic",
                  tokens_in: int = 0, tokens_out: int = 0) -> Dict[str, Any]:
    """Build an OpenAI-shaped response (used by the scripted provider and tests)."""
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_call is not None:
        message["tool_calls"] = [{
            "id": tool_call.id,
            "type": "function",
            "function": {"name": tool_call.name,
                         "arguments": json.dumps(tool_call.args, ensure_ascii=False)},
        }]
    return {
        "id": "agit-local",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_call else "stop"}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }
