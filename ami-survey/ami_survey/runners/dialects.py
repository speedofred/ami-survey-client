"""Provider dialects: the only per-vendor code in the system.

Every provider does the same three things - take a conversation plus tool
definitions, return a message that may contain tool calls, and report token
usage - but each spells them differently. A dialect owns that spelling and
nothing else, so the agent loop, the workspace and the survey submission stay
provider-agnostic.

To add a provider, implement `Dialect`: roughly forty lines, no changes anywhere
else.

The usage mapping is the part that matters for the benchmark, and it differs in a
way that silently corrupts comparisons if you get it wrong:

  * OpenAI     `prompt_tokens` INCLUDES cached tokens, so uncached = prompt - cached.
  * Anthropic  `input_tokens` EXCLUDES cached, so total input = input + cache read + cache write.
  * Gemini     `promptTokenCount` includes cached; thinking tokens are billed as
               output and are reported separately from `candidatesTokenCount`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..timeutil import iso


class DialectError(RuntimeError):
    pass


def _now() -> str:
    return iso(datetime.now(timezone.utc))


# --------------------------------------------------------------------------- #
# the neutral tool definitions, translated per provider
# --------------------------------------------------------------------------- #

TOOLS = [
    {
        "name": "list_files",
        "description": "List files and directories under a path in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory. Defaults to the root.",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file in the workspace, creating parent "
                       "directories. Overwrites an existing file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "mark_stage",
        "description": "Declare the workflow stage you are entering, at the moment you "
                       "enter it. Used to build the per-stage effort profile.",
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "description": "Name of the stage being entered."},
                "note": {"type": "string", "description": "Optional detail."},
            },
            "required": ["stage"],
        },
    },
]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    """One provider response, normalised."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = "unknown"
    call_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def call_record(self, start: str, end: str, index: int) -> dict:
        """A survey call record. Only provider-reported values are used."""
        uncached = max(
            self.input_tokens - self.cache_read_tokens - self.cache_write_tokens, 0
        )
        return {
            "call_id": self.call_id or f"call-{index}",
            "model": self.model,
            "start_time": start,
            "end_time": end,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_token_breakdown": {
                "uncached_input_tokens": uncached,
                "cache_creation_input_tokens": self.cache_write_tokens,
                "cache_read_input_tokens": self.cache_read_tokens,
            },
            "tool_calls": [{"name": tc.name} for tc in self.tool_calls],
            "has_text": bool(self.text),
            "has_thinking": self.reasoning_tokens > 0,
            # The count, not just the fact. It is already inside output_tokens -
            # every provider bills reasoning as output - so this does not change
            # any total; it says how much of the output was thinking rather than
            # answering, which is the difference a cost comparison turns on.
            "reasoning_output_tokens": self.reasoning_tokens,
        }


# --------------------------------------------------------------------------- #
# base
# --------------------------------------------------------------------------- #

class Dialect:
    """One provider's HTTP shape, plus the conversation state in that shape."""

    name = "base"
    default_base_url = ""
    key_env = ""
    #: how this provider counts input tokens, for the report's provenance
    usage_note = ""
    #: price-map namespace for this API, where the bare model id is ambiguous
    pricing_prefix: str | None = None

    def __init__(self, base_url: str, api_key: str, timeout: float = 300.0,
                 max_tokens: int = 8192):
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.system = ""
        self.history: list[dict] = []

    # -- conversation ------------------------------------------------------- #

    def begin(self, system: str, user: str) -> None:
        raise NotImplementedError

    def record_assistant(self, body: dict) -> None:
        raise NotImplementedError

    def record_tool_results(self, calls: list[ToolCall], results: list[str]) -> None:
        raise NotImplementedError

    # -- transport ---------------------------------------------------------- #

    def url(self, model: str) -> str:
        raise NotImplementedError

    def headers(self) -> dict:
        raise NotImplementedError

    def payload(self, model: str, tools: bool = True) -> dict:
        raise NotImplementedError

    def parse(self, body: dict) -> Turn:
        raise NotImplementedError

    def send(self, model: str, tools: bool = True) -> tuple[dict, str, str]:
        """POST one turn. Returns (body, start_time, end_time) - the timestamps are
        the client's own clock around the request, the only timing available."""
        data = json.dumps(self.payload(model, tools=tools)).encode()
        req = urllib.request.Request(self.url(model), data=data, method="POST")
        for key, value in self.headers().items():
            req.add_header(key, value)
        start = _now()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:500]
            raise DialectError(f"{self.name} returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise DialectError(f"cannot reach {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DialectError(f"{self.name} returned a non-JSON body: {exc}") from exc
        return body, start, _now()

    # -- one-shot grading call (outside the measured window) ----------------- #

    def ask_once(self, model: str, system: str, prompt: str) -> str:
        """A standalone question with no tools; returns the reply text."""
        saved_history, saved_system = self.history, self.system
        try:
            self.begin(system, prompt)
            body, _, _ = self.send(model, tools=False)
            return self.parse(body).text
        finally:
            self.history, self.system = saved_history, saved_system


# --------------------------------------------------------------------------- #
# OpenAI chat completions - and anything that speaks it
# --------------------------------------------------------------------------- #

class OpenAIDialect(Dialect):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    key_env = "OPENAI_API_KEY"
    usage_note = "prompt_tokens includes cached input; uncached = prompt - cached"

    def begin(self, system: str, user: str) -> None:
        self.system = system
        self.history = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def url(self, model: str) -> str:
        return f"{self.base_url}/chat/completions"

    def headers(self) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"}

    def payload(self, model: str, tools: bool = True) -> dict:
        body: dict = {"model": model, "messages": self.history}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in TOOLS]
            body["tool_choice"] = "auto"
        return body

    def _message(self, body: dict) -> dict:
        return ((body.get("choices") or [{}])[0]).get("message") or {}

    def parse(self, body: dict) -> Turn:
        message = self._message(body)
        usage = body.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_arguments__": fn.get("arguments")}
            calls.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", args=args))
        return Turn(
            text=message.get("content") or "",
            tool_calls=calls,
            model=body.get("model") or "unknown",
            call_id=body.get("id") or "",
            input_tokens=prompt,
            output_tokens=int(usage.get("completion_tokens") or 0),
            cache_read_tokens=cached,
            reasoning_tokens=int(
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            ),
        )

    def record_assistant(self, body: dict) -> None:
        message = self._message(body)
        self.history.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls") or None,
            }
        )

    def record_tool_results(self, calls: list[ToolCall], results: list[str]) -> None:
        for call, result in zip(calls, results):
            self.history.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )


# --------------------------------------------------------------------------- #
# Anthropic messages
# --------------------------------------------------------------------------- #

class AnthropicDialect(Dialect):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"
    key_env = "ANTHROPIC_API_KEY"
    usage_note = "input_tokens excludes cached; total input = input + cache read + cache write"
    api_version = "2023-06-01"

    def begin(self, system: str, user: str) -> None:
        self.system = system
        self.history = [{"role": "user", "content": user}]

    def url(self, model: str) -> str:
        return f"{self.base_url}/messages"

    def headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
        }

    def payload(self, model: str, tools: bool = True) -> dict:
        body: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": self.history,
        }
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t["description"],
                 "input_schema": t["parameters"]}
                for t in TOOLS
            ]
        return body

    def parse(self, body: dict) -> Turn:
        usage = body.get("usage") or {}
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        text_parts, calls = [], []
        for block in body.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        args=block.get("input") or {},
                    )
                )
        return Turn(
            text="".join(text_parts),
            tool_calls=calls,
            model=body.get("model") or "unknown",
            call_id=body.get("id") or "",
            # Anthropic reports uncached input only, so the parts are summed.
            input_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    def record_assistant(self, body: dict) -> None:
        self.history.append({"role": "assistant", "content": body.get("content") or []})

    def record_tool_results(self, calls: list[ToolCall], results: list[str]) -> None:
        self.history.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": result}
                    for call, result in zip(calls, results)
                ],
            }
        )


# --------------------------------------------------------------------------- #
# Google Gemini generateContent
# --------------------------------------------------------------------------- #

class GeminiDialect(Dialect):
    name = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    key_env = "GEMINI_API_KEY"
    # bare "gemini-2.5-pro" resolves to the Vertex entry in the price map
    pricing_prefix = "gemini"
    usage_note = ("promptTokenCount includes cached input; thinking tokens are billed "
                  "as output and added to candidatesTokenCount")

    def begin(self, system: str, user: str) -> None:
        self.system = system
        self.history = [{"role": "user", "parts": [{"text": user}]}]

    def url(self, model: str) -> str:
        return f"{self.base_url}/models/{model}:generateContent"

    def headers(self) -> dict:
        return {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

    def payload(self, model: str, tools: bool = True) -> dict:
        body: dict = {
            "contents": self.history,
            "systemInstruction": {"parts": [{"text": self.system}]},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": TOOLS}]
        return body

    def _parts(self, body: dict) -> list[dict]:
        candidates = body.get("candidates") or [{}]
        return (candidates[0].get("content") or {}).get("parts") or []

    def parse(self, body: dict) -> Turn:
        usage = body.get("usageMetadata") or {}
        thoughts = int(usage.get("thoughtsTokenCount") or 0)
        text_parts, calls = [], []
        for i, part in enumerate(self._parts(body)):
            if "text" in part:
                text_parts.append(part.get("text") or "")
            elif "functionCall" in part:
                fc = part["functionCall"] or {}
                calls.append(
                    ToolCall(
                        # Gemini matches function responses by name, not id.
                        id=fc.get("name", "") + f"-{i}",
                        name=fc.get("name") or "",
                        args=fc.get("args") or {},
                    )
                )
        return Turn(
            text="".join(text_parts),
            tool_calls=calls,
            model=body.get("modelVersion") or "unknown",
            call_id=body.get("responseId") or "",
            input_tokens=int(usage.get("promptTokenCount") or 0),
            # thinking tokens are output tokens that are reported separately
            output_tokens=int(usage.get("candidatesTokenCount") or 0) + thoughts,
            cache_read_tokens=int(usage.get("cachedContentTokenCount") or 0),
            reasoning_tokens=thoughts,
        )

    def record_assistant(self, body: dict) -> None:
        self.history.append({"role": "model", "parts": self._parts(body)})

    def record_tool_results(self, calls: list[ToolCall], results: list[str]) -> None:
        self.history.append(
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": call.name,
                            "response": {"result": result},
                        }
                    }
                    for call, result in zip(calls, results)
                ],
            }
        )


DIALECTS: dict[str, type[Dialect]] = {
    d.name: d for d in (OpenAIDialect, AnthropicDialect, GeminiDialect)
}


def get(name: str) -> type[Dialect]:
    try:
        return DIALECTS[name.lower()]
    except KeyError:
        raise DialectError(
            f"unknown provider {name!r}. Available: {', '.join(sorted(DIALECTS))}"
        ) from None
