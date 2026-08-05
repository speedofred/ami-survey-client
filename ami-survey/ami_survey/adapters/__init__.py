"""Runtime telemetry adapters, and the registry that picks one.

An adapter turns a runtime's own record of its API calls into the normalised call
records the survey API ingests. The survey is model-agnostic - the model id and
its price are read from whatever the runtime recorded - but *measurement* is
runtime-specific, because each agent harness logs its sessions differently. So
there is one adapter per harness, not per model, and the agent taking the survey
never has to say which one it is: `detect()` works that out.

Normalised call record:
    call_id, model, start_time, end_time, duration_seconds,
    input_tokens, output_tokens, input_token_breakdown{...},
    tool_calls[{name, command?}], has_text, has_thinking, source, evidence{...}

Each adapter module exposes:
    NAME              short id, e.g. "claude_code"
    LABEL             what goes in `telemetry_adapter`, e.g. "claude_code_transcript"
    locate(cwd, session_id=None, transcript_path=None) -> Path
                      the session log this run should be measured from; raises
                      TelemetryNotFound if this runtime is not the one in use
    probe(...)        runtime identity + proposed measurement window
    collect(...)      the full telemetry payload

A runtime with no adapter is not stuck: it can post its own usage records
directly via `ami_record_calls` / POST /runs/{id}/calls. It just has to have them.
"""

from __future__ import annotations

import os
from pathlib import Path


class TelemetryNotFound(RuntimeError):
    """No session log for this runtime - it is probably not the one running."""


def _modules() -> list:
    from . import claude_code, codex

    return [claude_code, codex]


def available() -> list[str]:
    return [m.NAME for m in _modules()]


def _located(cwd: str | None, session_id: str | None, transcript_path: str | None):
    """Every adapter that can find a session for this run, newest log first."""
    found = []
    for module in _modules():
        # An explicitly supplied log belongs to exactly one runtime; adapters that
        # can recognise their own format get to disown it rather than mis-parse it.
        if transcript_path and hasattr(module, "claims"):
            if not module.claims(transcript_path):
                continue
        try:
            path = module.locate(
                cwd=cwd, session_id=session_id, transcript_path=transcript_path
            )
        except (TelemetryNotFound, OSError):
            continue
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0.0
        found.append((mtime, module, path))
    return sorted(found, key=lambda f: f[0], reverse=True)


def _environment_candidates() -> list:
    """Narrow the field using the environment the runtime gave this process.

    Agent harnesses launch the MCP server as a child process and stamp their own
    variables on it, which identifies the asking runtime exactly. That beats every
    heuristic: two runtimes can have sessions in one directory, and the most
    recently written log is not always the one asking. Adapters that cannot tell
    either way stay in the running.
    """
    verdicts = [(m, m.runs_here() if hasattr(m, "runs_here") else None) for m in _modules()]
    positive = [m for m, v in verdicts if v is True]
    if positive:
        return positive
    undecided = [m for m, v in verdicts if v is not False]
    return undecided or [m for m, _ in verdicts]


def detect(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    adapter: str | None = None,
):
    """Return the adapter module that should measure this run.

    Explicit choice wins (argument, then AMI_ADAPTER), then the runtime named by
    this process's environment. Failing both, every remaining adapter is asked
    whether it can find a session for this working directory and the most
    recently written log wins.
    """
    wanted = adapter or os.environ.get("AMI_ADAPTER")
    if wanted:
        for module in _modules():
            if module.NAME == wanted:
                return module
        raise TelemetryNotFound(
            f"Unknown adapter {wanted!r}. Available: {', '.join(available())}"
        )

    found = _located(cwd, session_id, transcript_path)
    by_environment = {m.NAME for m in _environment_candidates()}
    narrowed = [f for f in found if f[1].NAME in by_environment]
    found = narrowed or found
    if not found:
        raise TelemetryNotFound(
            "No session log found for any known runtime "
            f"({', '.join(available())}) in {cwd or os.getcwd()!r}. If that is not "
            "your workspace directory - the survey server may have been launched "
            "elsewhere - call this again with cwd set to the directory you are "
            "working in. If your runtime has no adapter, post its own usage records "
            "with ami_record_calls instead: the survey does not accept estimates."
        )
    return found[0][1]


def probe(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    adapter: str | None = None,
) -> dict:
    module = detect(cwd, session_id, transcript_path, adapter)
    return module.probe(cwd=cwd, session_id=session_id, transcript_path=transcript_path)


def collect(
    cwd: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    adapter: str | None = None,
) -> dict:
    module = detect(cwd, session_id, transcript_path, adapter)
    return module.collect(
        cwd=cwd,
        session_id=session_id,
        transcript_path=transcript_path,
        window_start=window_start,
        window_end=window_end,
    )
