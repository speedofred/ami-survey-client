"""Telemetry adapter for the Codex runtime.

Codex appends a JSONL rollout per session under
`~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<timestamp>-<session-id>.jsonl`.
The file carries everything the survey needs, so no database is read: the
session's own `session_meta` line records the cwd and runtime identity, and each
API call ends with an `event_msg/token_count` whose `info.last_token_usage` is
the usage the provider reported for that call.

Three details matter for correctness:

* Usage is reported per call in `last_token_usage`, alongside a running
  `total_token_usage`. Summing the running total would multiply-count, so only
  `last_token_usage` is read - one call record per `token_count` event.
* `input_tokens` here follows the Responses API convention and *includes* the
  cached portion, unlike Anthropic's Messages API where it excludes it. The
  breakdown is therefore uncached = input - cached - cache_write.
* There is no recorded request duration. A call is measured from what triggered
  it (the human turn, or the tool output that resumed the loop) to the last item
  the model produced before its `token_count` - deliberately excluding the tool
  execution that follows, which is not model latency. This is labelled
  `trigger_to_completion`, the same basis the Claude Code adapter reports.

Human turns come from `event_msg/user_message` events. That is an exact signal
rather than a heuristic: injected context arrives as `response_item/message`
with role `user`, which is a different record and is correctly ignored.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
from pathlib import Path

from .. import config
from ..timeutil import iso as _iso, parse_ts
from . import TelemetryNotFound

MAX_COMMAND_CHARS = 120

NAME = "codex"
LABEL = "codex_rollout"

#: model-produced items; anything else in a turn is context or tool plumbing
_MODEL_ITEMS = ("reasoning", "custom_tool_call", "function_call", "local_shell_call")
#: events that resume the agent loop, i.e. trigger the next API call
_TRIGGERS = ("user_message", "custom_tool_call_output", "function_call_output")
#: the shell command inside an exec tool call: tools.exec_command({"cmd": "..."})
_EXEC_CMD = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')


class RolloutNotFound(TelemetryNotFound):
    pass


def sessions_dir() -> Path:
    raw = os.environ.get("AMI_CODEX_SESSIONS_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def environment_session_id() -> str | None:
    """The thread id Codex puts in the environment of processes it spawns."""
    return os.environ.get("CODEX_THREAD_ID") or None


def runs_here() -> bool | None:
    """Is Codex the runtime that launched this process?

    True when Codex has stamped its variables on us. Otherwise `None`, not False:
    an MCP server may be started once and reused across threads, in which case
    the absence of these variables proves nothing. Saying "I cannot tell" keeps
    this adapter in the running instead of ruling it out on a guess.
    """
    if any(k.startswith("CODEX_") for k in os.environ):
        return True
    return None


def _entries(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a partially flushed final line
    return out


def _payload(entry: dict) -> dict:
    p = entry.get("payload")
    return p if isinstance(p, dict) else {}


def _kind(entry: dict) -> str:
    """`event_msg/token_count` and friends, flattened to one string."""
    return _payload(entry).get("type") or entry.get("type") or ""


def claims(path: Path) -> bool:
    """Is this file one of ours? Used when a transcript path is given explicitly."""
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for _, line in zip(range(5), fh):
                if line.strip() and '"session_meta"' in line:
                    return True
    except OSError:
        return False
    return False


def session_meta(entries: list[dict]) -> dict:
    for e in entries:
        if e.get("type") == "session_meta":
            return _payload(e)
    return {}


def locate(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
) -> Path:
    """Find the rollout for the session asking for the survey.

    Explicit path, then explicit session id, then the most recently modified
    rollout recorded in `cwd`. A Codex window opened on a subdirectory of the
    project (or the project root, with the work in a subdirectory) still matches:
    the survey is being taken about that session either way.
    """
    explicit = transcript_path or os.environ.get("AMI_TRANSCRIPT_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise RolloutNotFound(f"transcript_path does not exist: {p}")
        if not claims(p):
            raise RolloutNotFound(f"{p} is not a Codex rollout.")
        return p

    root = sessions_dir()
    if not root.is_dir():
        raise RolloutNotFound(
            f"No Codex sessions directory at {root}. If this agent is not running "
            "in Codex, record calls with ami_record_calls instead."
        )

    if session_id:
        matches = list(root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
        if not matches:
            raise RolloutNotFound(f"No Codex rollout found for session_id {session_id}.")
        return max(matches, key=lambda p: p.stat().st_mtime)

    candidates = sessions(cwd)
    if not candidates:
        raise RolloutNotFound(
            f"No Codex rollout found for cwd {str(Path(cwd or os.getcwd()).resolve())!r} "
            f"under {root}."
        )

    # Prefer the thread named in the environment, but only among the rollouts that
    # already match this directory. A reused server process could be carrying the
    # variables of an older thread, and a stale pin that silently measured the
    # wrong session would be worse than falling back to the newest log.
    from_env = environment_session_id()
    if from_env:
        pinned = next((p for p in candidates if from_env in p.name), None)
        if pinned is not None:
            return pinned
    return candidates[0]


def is_subagent(meta: dict) -> bool:
    """True for a rollout written by a subagent rather than by the human's session.

    Codex spawns its own subagents - an approval reviewer, for one - and each
    writes its own rollout in the same directory, on its own model. Those logs are
    newer than the session that spawned them, so without this check the survey
    measures a review subagent's single call instead of the workflow.
    """
    if meta.get("thread_source") == "subagent":
        return True
    # `source` is a plain string for a human session ("vscode", "cli") and a
    # structured value like {"subagent": {...}} for a spawned one.
    return not isinstance(meta.get("source", ""), str)


def sessions(cwd: str | None = None, include_subagents: bool = False) -> list[Path]:
    """Every rollout recorded for this directory, most recently written first.

    Subagent rollouts are excluded: the survey is about the session the human is
    talking to, not about the helpers it spawned.
    """
    root = sessions_dir()
    if not root.is_dir():
        return []
    cwd = str(Path(cwd or os.getcwd()).resolve())
    found: list[Path] = []
    for p in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            with p.open(encoding="utf-8") as fh:
                first = fh.readline()
        except OSError:
            continue
        if '"session_meta"' not in first:
            continue
        try:
            meta = json.loads(first).get("payload") or {}
        except json.JSONDecodeError:
            continue
        if not include_subagents and is_subagent(meta):
            continue
        recorded = meta.get("cwd")
        if not recorded:
            continue
        recorded = str(Path(recorded))
        if recorded == cwd or recorded.startswith(cwd + os.sep) or cwd.startswith(
            recorded + os.sep
        ):
            found.append(p)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _source_label(meta: dict) -> str | None:
    """A readable `source`. It is a plain string for a human session, but a nested
    object for a spawned one, which must not be dropped into the platform label raw.
    """
    source = meta.get("source")
    if isinstance(source, str) and source:
        return source
    if isinstance(source, dict):
        kind, value = next(iter(source.items()), (None, None))
        if isinstance(value, dict):
            detail = next((v for v in value.values() if isinstance(v, str)), None)
            return f"{kind}:{detail}" if detail else str(kind)
        return f"{kind}:{value}" if value else str(kind)
    originator = meta.get("originator")
    return originator if isinstance(originator, str) else None


def runtime_metadata(entries: list[dict], rollout: Path) -> dict:
    """Platform / runtime identity, read from the rollout's own metadata."""
    meta = session_meta(entries)
    version = meta.get("cli_version")
    source = _source_label(meta)
    platform = "codex"
    if source:
        platform += f"/{source}"
    if version:
        platform += f"@{version}"
    return {
        "platform": platform,
        # The survey server sees only what the adapter records, and `platform`
        # is identical on every operating system - so without this a
        # cross-platform benchmark cannot tell one from another. The MCP
        # server runs on the agent's own machine, so this is that machine.
        "os": _platform.system(),
        "runtime": "codex",
        "entrypoint": source,
        "is_subagent": is_subagent(meta),
        "originator": meta.get("originator"),
        "version": version,
        "session_id": meta.get("session_id"),
        "model_provider": meta.get("model_provider"),
        "transcript_path": str(rollout),
    }


def _model_at(entries: list[dict], index: int) -> str | None:
    """The model in force for the call ending at `index`.

    Read from the most recent `turn_context`, so a session where the model was
    switched part-way through attributes each call to what actually served it.
    """
    for e in reversed(entries[: index + 1]):
        if e.get("type") == "turn_context":
            model = _payload(e).get("model")
            if model:
                return model
    for e in entries:
        if e.get("type") == "turn_context" and _payload(e).get("model"):
            return _payload(e)["model"]
    return None


def _tool_call(payload: dict) -> dict:
    """One tool call, with the shell command extracted where there is one.

    Codex's `exec` tool takes a JS snippet that calls `tools.exec_command`, so
    the command is dug out of it: without that every shell call would classify as
    generic execution, and the effort profile would not be comparable with a
    runtime that records commands plainly.
    """
    name = payload.get("name") or "unknown"
    entry: dict = {"name": name}
    raw = payload.get("input")
    if isinstance(raw, str) and raw:
        match = _EXEC_CMD.search(raw)
        if match:
            try:
                command = json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                command = match.group(1)
            entry["command"] = command[:MAX_COMMAND_CHARS]
    return entry


def user_prompt_times(entries: list[dict]) -> list[str]:
    """Genuine human turns. `event_msg/user_message` is emitted only for these."""
    return [
        _iso(parse_ts(e["timestamp"]))
        for e in entries
        if _kind(e) == "user_message" and e.get("timestamp")
    ]


def first_user_prompt_time(entries: list[dict]) -> str | None:
    times = user_prompt_times(entries)
    return times[0] if times else None


def extract_calls(
    entries: list[dict],
    rollout: Path,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[dict]:
    """One normalised call record per API request in the window."""
    meta = session_meta(entries)
    triggers: list[tuple[int, str]] = []
    pending: list[tuple[int, dict]] = []  # model-produced items since the last call
    calls: list[dict] = []

    for i, e in enumerate(entries):
        kind = _kind(e)
        ts = e.get("timestamp")

        if kind in _TRIGGERS and ts:
            triggers.append((i, ts))
            continue
        if kind == "task_started" and ts:
            triggers.append((i, ts))
            continue

        if e.get("type") == "response_item":
            payload = _payload(e)
            ptype = payload.get("type")
            if ptype in _MODEL_ITEMS or (
                ptype == "message" and payload.get("role") == "assistant"
            ):
                pending.append((i, e))
            continue

        if kind != "token_count":
            continue

        usage = (_payload(e).get("info") or {}).get("last_token_usage") or {}
        if not usage:
            pending = []
            continue

        end_ts = ts
        if pending:
            end_ts = max(
                (p[1].get("timestamp") for p in pending if p[1].get("timestamp")),
                default=ts,
            )
        first_index = pending[0][0] if pending else i
        start_ts = next(
            (t for idx, t in reversed(triggers) if idx < first_index), None
        )
        duration_measured = start_ts is not None
        if start_ts is None:
            start_ts = end_ts

        start_dt, end_dt = parse_ts(start_ts), parse_ts(end_ts)
        if start_dt > end_dt:
            start_dt = end_dt

        items = [p[1] for p in pending]
        pending = []

        if window_start and end_dt < parse_ts(window_start):
            continue
        if window_end and end_dt > parse_ts(window_end):
            continue

        total_input = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cached_input_tokens") or 0)
        cache_write = int(usage.get("cache_write_input_tokens") or 0)
        reasoning = int(usage.get("reasoning_output_tokens") or 0)

        tool_calls, has_text = [], False
        for item in items:
            payload = _payload(item)
            ptype = payload.get("type")
            if ptype in _MODEL_ITEMS and ptype != "reasoning":
                tool_calls.append(_tool_call(payload))
            elif ptype == "message":
                has_text = True

        calls.append(
            {
                "call_id": f"{meta.get('session_id', 'codex')}-call-{len(calls) + 1}",
                "model": _model_at(entries, i),
                "start_time": _iso(start_dt),
                "end_time": _iso(end_dt),
                "duration_seconds": round((end_dt - start_dt).total_seconds(), 3),
                "duration_basis": (
                    "trigger_to_completion" if duration_measured else "unmeasured"
                ),
                "input_tokens": total_input,
                "output_tokens": int(usage.get("output_tokens") or 0),
                "input_token_breakdown": {
                    # Responses API: input_tokens already includes the cached part.
                    "uncached_input_tokens": max(
                        total_input - cache_read - cache_write, 0
                    ),
                    "cache_creation_input_tokens": cache_write,
                    "cache_read_input_tokens": cache_read,
                },
                "tool_calls": tool_calls,
                "has_text": has_text,
                "has_thinking": reasoning > 0,
                # Promoted out of `evidence`: this is a measurement the provider
                # reported, not a note about where the measurement came from.
                "reasoning_output_tokens": reasoning,
                "is_sidechain": False,
                "source": LABEL,
                "evidence": {
                    "transcript_path": str(rollout),
                    "session_id": meta.get("session_id"),
                    "token_count_index": i,
                },
            }
        )
    return calls


def suggest_window(entries: list[dict], calls: list[dict] | None = None) -> dict:
    """Propose the measurement window for the workflow that just finished.

    Start: the first human turn of the session.
    End:   the human turn that asked for the survey - so the survey's own token
           spend is excluded from the workflow's figures. If no agent call
           completed before that turn, the window stays open to now instead.
    """
    prompts = user_prompt_times(entries)
    calls = calls if calls is not None else extract_calls(entries, Path("."))
    start = prompts[0] if prompts else (calls[0]["start_time"] if calls else None)
    start_basis = (
        "first human turn of the session" if prompts else "earliest observed agent call"
    )

    end, end_basis = None, "survey start time (no separate survey request turn)"
    if prompts:
        last_prompt = prompts[-1]
        if any(parse_ts(c["end_time"]) <= parse_ts(last_prompt) for c in calls):
            end, end_basis = last_prompt, "the human turn that requested the survey"
    return {
        "start": start,
        "start_basis": start_basis,
        "end": end,
        "end_basis": end_basis,
        "human_turn_count": len(prompts),
    }


def collect(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    """Full telemetry payload for the current Codex session."""
    path = locate(cwd=cwd, session_id=session_id, transcript_path=transcript_path)
    entries = _entries(path)
    all_calls = extract_calls(entries, path)
    calls = extract_calls(entries, path, window_start, window_end)
    return {
        "adapter": LABEL,
        "runtime_metadata": runtime_metadata(entries, path),
        "default_workflow_start_time": first_user_prompt_time(entries),
        "suggested_window": suggest_window(entries, all_calls),
        "window": {"start": window_start, "end": window_end},
        "transcript_entry_count": len(entries),
        "calls_in_session": len(all_calls),
        "calls": calls,
    }


def probe(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
) -> dict:
    """Runtime identity and a proposed measurement window, without ingesting calls."""
    path = locate(cwd=cwd, session_id=session_id, transcript_path=transcript_path)
    entries = _entries(path)
    calls = extract_calls(entries, path)
    return {
        "adapter": LABEL,
        "runtime_metadata": runtime_metadata(entries, path),
        "suggested_window": suggest_window(entries, calls),
        "calls_in_session": len(calls),
    }
