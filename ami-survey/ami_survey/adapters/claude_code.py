"""Telemetry adapter for the Claude Code runtime.

Claude Code appends a JSONL transcript per session under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. Every assistant entry
carries the provider's own `usage` block, the model id, the request id and a
timestamp. That file is the concrete measurement source for this runtime - the
agent does not report its own token counts, it reads back what the API charged.

Two details matter for correctness:

* One API response is written as *several* transcript entries (one per content
  block: thinking, text, each tool_use). They share a `requestId` and repeat the
  same `usage` object. Summing entries would multiply-count tokens, so entries
  are grouped by request id and counted once.
* There is no recorded request duration. Per-call elapsed time is measured as
  the wall-clock gap between the entry that triggered the request (the user turn
  or tool result immediately preceding it) and the completion of the response.
  This is labelled as such in the provenance rather than presented as a
  provider-reported latency.
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

NAME = "claude_code"
LABEL = "claude_code_transcript"


class TranscriptNotFound(TelemetryNotFound):
    """Kept as its own name for callers that catch it; the registry catches the base."""


def encode_cwd(path: str) -> str:
    """Claude Code's project-directory encoding: non-alphanumerics become dashes."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


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


def environment_session_id() -> str | None:
    """The session id Claude Code puts in the environment of processes it spawns.

    The MCP server is one of those processes, so this identifies the asking
    session exactly - no guessing from modification times, and no confusion with
    other sessions running in the same directory.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def runs_here() -> bool:
    """Is Claude Code the runtime that launched this process?

    Claude Code sets CLAUDECODE for its children, so its absence is a positive
    signal that some other runtime is asking.
    """
    return bool(os.environ.get("CLAUDECODE") or environment_session_id())


def claims(path) -> bool:
    """Is this file one of ours? Used when a transcript path is given explicitly.

    A Claude Code transcript is JSONL of entries carrying `sessionId`/`uuid`; a
    Codex rollout opens with a `session_meta` record. Without this check an
    explicitly supplied log is parsed by whichever adapter is asked first.
    """
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for _, line in zip(range(5), fh):
                line = line.strip()
                if not line:
                    continue
                if '"session_meta"' in line:
                    return False
                if '"sessionId"' in line or '"uuid"' in line:
                    return True
    except OSError:
        return False
    return False


def find_transcript(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
) -> Path:
    """Locate the transcript for the session that is asking for the survey.

    Preference order: an explicit path, an explicit session id, then the most
    recently modified transcript whose entries were recorded in `cwd`.
    """
    explicit = transcript_path or os.environ.get("AMI_TRANSCRIPT_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise TranscriptNotFound(f"transcript_path does not exist: {p}")
        return p

    root = config.CLAUDE_PROJECTS_DIR
    if not root.exists():
        raise TranscriptNotFound(
            f"No Claude Code transcripts directory at {root}. If this agent is not "
            "running in Claude Code, record calls with ami_record_calls instead."
        )

    if session_id:
        matches = list(root.glob(f"*/{session_id}.jsonl"))
        if not matches:
            raise TranscriptNotFound(f"No transcript found for session_id {session_id}.")
        return matches[0]

    # The asking session names itself in the environment. Prefer that over any
    # directory heuristic: several sessions can share a directory, and the newest
    # log is not necessarily the one taking the survey.
    from_env = environment_session_id()
    if from_env:
        matches = list(root.glob(f"*/{from_env}.jsonl"))
        if matches:
            return matches[0]

    candidates = sessions(cwd)
    if not candidates:
        raise TranscriptNotFound(
            f"No Claude Code transcript found for cwd {cwd or os.getcwd()!r} under {root}."
        )
    return candidates[0]


def sessions(cwd: str | None = None) -> list[Path]:
    """Every transcript recorded for this directory, most recently written first."""
    root = config.CLAUDE_PROJECTS_DIR
    if not root.is_dir():
        return []
    cwd = cwd or os.getcwd()
    candidates: list[Path] = []

    encoded = root / encode_cwd(cwd)
    if encoded.is_dir():
        candidates = list(encoded.glob("*.jsonl"))

    if not candidates:  # fall back to reading the cwd recorded inside each transcript
        for p in root.glob("*/*.jsonl"):
            try:
                with p.open(encoding="utf-8") as fh:
                    for _, line in zip(range(40), fh):
                        if f'"cwd":"{cwd}"' in line.replace(", ", ",").replace('": "', '":"'):
                            candidates.append(p)
                            break
            except OSError:
                continue

    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def _tool_calls(content: list) -> list[dict]:
    calls = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        entry: dict = {"name": name}
        inp = block.get("input") or {}
        if isinstance(inp, dict) and name.lower() in ("bash", "shell"):
            cmd = str(inp.get("command", ""))[:MAX_COMMAND_CHARS]
            if cmd:
                entry["command"] = cmd
        calls.append(entry)
    return calls


def runtime_metadata(entries: list[dict], transcript: Path) -> dict:
    """Platform / runtime identity, read from the transcript's own metadata."""
    version = entrypoint = session = git_branch = None
    for e in entries:
        version = version or e.get("version")
        entrypoint = entrypoint or e.get("entrypoint")
        session = session or e.get("sessionId")
        git_branch = git_branch or e.get("gitBranch")
        if version and entrypoint and session:
            break
    platform = "claude-code"
    if entrypoint:
        platform += f"/{entrypoint}"
    if version:
        platform += f"@{version}"
    return {
        "platform": platform,
        # The survey server sees only what the adapter records, and `platform`
        # is identical on every operating system - so without this a
        # cross-platform benchmark cannot tell one from another. The MCP
        # server runs on the agent's own machine, so this is that machine.
        "os": _platform.system(),
        "runtime": "claude-code",
        "entrypoint": entrypoint,
        "version": version,
        "session_id": session,
        "git_branch": git_branch,
        "transcript_path": str(transcript),
    }


def _is_user_prompt(e: dict) -> bool:
    """True for a genuine human turn (not a tool result or injected reminder)."""
    if e.get("type") != "user" or e.get("toolUseResult") is not None or e.get("isMeta"):
        return False
    if not e.get("timestamp"):
        return False
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    else:
        text = " ".join(
            b.get("text", "")
            for b in (content or [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
    stripped = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    return bool(stripped.strip())


def user_prompt_times(entries: list[dict]) -> list[str]:
    return [_iso(parse_ts(e["timestamp"])) for e in entries if _is_user_prompt(e)]


def first_user_prompt_time(entries: list[dict]) -> str | None:
    times = user_prompt_times(entries)
    return times[0] if times else None


def suggest_window(entries: list[dict], calls: list[dict] | None = None) -> dict:
    """Propose the measurement window for the workflow that just finished.

    Start: the first human turn of the session.
    End:   the human turn that asked for the survey - so the survey's own token
           spend is excluded from the workflow's figures. If no agent call
           completed before that turn (i.e. the survey was requested in the same
           turn as the work, or the agent invoked the survey itself), the window
           stays open to now instead.
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


def extract_calls(
    entries: list[dict],
    transcript: Path,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[dict]:
    """Return one normalised call record per API request in the window."""
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in entries:
        if e.get("type") != "assistant" or e.get("isApiErrorMessage"):
            continue
        msg = e.get("message") or {}
        model = msg.get("model")
        if not model or model.startswith("<"):
            continue  # synthetic/local entries were never billed API calls
        key = e.get("requestId") or msg.get("id") or e.get("uuid")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)

    calls: list[dict] = []
    for key in order:
        grp = sorted(groups[key], key=lambda e: e["timestamp"])
        first, last = grp[0], grp[-1]
        msg = first.get("message") or {}
        usage = msg.get("usage") or {}

        end_dt = parse_ts(last["timestamp"])

        # Walk back past same-request entries to find what triggered the request.
        anchor, seen = first, {first.get("uuid")}
        while True:
            parent = by_uuid.get(anchor.get("parentUuid"))
            if parent is None or parent.get("uuid") in seen:
                break
            seen.add(parent.get("uuid"))
            same_request = (
                parent.get("type") == "assistant"
                and (parent.get("requestId") or (parent.get("message") or {}).get("id")) == key
            )
            if not same_request:
                anchor = parent
                break
            anchor = parent
        start_dt = parse_ts(anchor["timestamp"]) if anchor is not first else end_dt
        if start_dt > end_dt:
            start_dt = end_dt
        duration_measured = anchor is not first

        if window_start and end_dt < parse_ts(window_start):
            continue
        if window_end and end_dt > parse_ts(window_end):
            continue

        uncached = int(usage.get("input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)

        tool_calls: list[dict] = []
        has_text = has_thinking = False
        for e in grp:
            content = (e.get("message") or {}).get("content") or []
            if isinstance(content, str):
                has_text = has_text or bool(content.strip())
                continue
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    has_text = True
                elif btype in ("thinking", "redacted_thinking"):
                    has_thinking = True
            tool_calls.extend(_tool_calls(content))

        calls.append(
            {
                "call_id": key,
                "model": msg.get("model"),
                "start_time": _iso(start_dt),
                "end_time": _iso(end_dt),
                "duration_seconds": round((end_dt - start_dt).total_seconds(), 3),
                "duration_basis": "trigger_to_completion" if duration_measured else "unmeasured",
                "input_tokens": uncached + cache_write + cache_read,
                "output_tokens": output,
                "input_token_breakdown": {
                    "uncached_input_tokens": uncached,
                    "cache_creation_input_tokens": cache_write,
                    "cache_read_input_tokens": cache_read,
                },
                "tool_calls": tool_calls,
                "has_text": has_text,
                "has_thinking": has_thinking,
                "is_sidechain": bool(first.get("isSidechain")),
                "service_tier": usage.get("service_tier"),
                "source": "claude_code_transcript",
                "evidence": {
                    "transcript_path": str(transcript),
                    "session_id": first.get("sessionId"),
                    "request_id": first.get("requestId"),
                    "entry_uuids": [e.get("uuid") for e in grp],
                },
            }
        )
    return calls


#: registry entry point - "can this runtime find a session for this run?"
locate = find_transcript


def collect(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    """Full telemetry payload for the current Claude Code session."""
    path = find_transcript(cwd=cwd, session_id=session_id, transcript_path=transcript_path)
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
    path = find_transcript(cwd=cwd, session_id=session_id, transcript_path=transcript_path)
    entries = _entries(path)
    calls = extract_calls(entries, path)
    return {
        "adapter": LABEL,
        "runtime_metadata": runtime_metadata(entries, path),
        "suggested_window": suggest_window(entries, calls),
        "calls_in_session": len(calls),
    }
