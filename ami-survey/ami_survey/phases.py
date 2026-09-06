"""Execution-phase attribution for individual agent calls.

Two independent attributions exist, matching the collection inventory:

* `workflow_stage`      - what the workflow *declared* it was doing, via explicit
                          stage markers the agent emits during the run. High
                          provenance, but only available if markers were used.
* `observed_execution_phase` - what AMI *observed* the call doing, classified
                          from the runtime behaviour of the call (which tools it
                          invoked). Always available, lower provenance.

Classification is deliberately mechanical: it reads only the tool names (and, for
shell calls, the command string) recorded for the call. It never asks a model what
it thinks it was doing.
"""

from __future__ import annotations

import re

from .timeutil import parse_ts

# Observed phases, in precedence order (first match wins when a call mixes phases).
PHASE_PRECEDENCE = ["Execution", "Verification", "Discovery", "Planning", "Reporting"]

DISCOVERY_TOOLS = {
    "read", "glob", "grep", "ls", "webfetch", "websearch", "notebookread",
    "task", "agent", "explore", "toolsearch", "readmcpresource", "listmcpresources",
    "get_page_text", "read_page", "read_console_messages", "read_network_requests",
    "find", "taskget", "tasklist", "taskoutput", "cronlist",
}
PLANNING_TOOLS = {
    "todowrite", "exitplanmode", "enterplanmode", "taskcreate", "taskupdate",
    "schedulewakeup", "mark_chapter", "skill",
}
VERIFICATION_TOOLS = {"reportfindings", "monitor"}
EXECUTION_TOOLS = {
    "write", "edit", "multiedit", "notebookedit", "artifact", "senduserfile",
    "sendmessage", "croncreate", "crondelete", "form_input", "computer", "navigate",
    "pushnotification", "remotetrigger",
}

# Shell commands whose purpose is to check work rather than change it.
_VERIFICATION_CMD = re.compile(
    r"\b("
    r"pytest|unittest|nose|jest|vitest|mocha|tsc|mypy|ruff|flake8|pylint|eslint|"
    r"golangci-lint|shellcheck|rubocop|clippy|"
    r"npm\s+(test|run\s+(test|lint|typecheck|check))|"
    r"yarn\s+(test|lint)|pnpm\s+(test|lint)|"
    r"cargo\s+(test|check|clippy)|go\s+(test|vet)|"
    r"make\s+(test|check|lint)|gradle\s+test|mvn\s+test|dotnet\s+test|"
    r"bats|tox|nox"
    r")\b",
    re.IGNORECASE,
)
# Shell commands that only look at state.
_READONLY_CMD = re.compile(
    r"^\s*(ls|cat|head|tail|find|grep|rg|wc|stat|file|du|df|tree|which|pwd|echo|"
    r"git\s+(status|log|diff|show|branch|remote)|curl\s+-s?I)\b",
    re.IGNORECASE,
)


def _shell_phase(command: str | None) -> str:
    """Classify a shell invocation by its constituent commands.

    Real commands are compound - `cd x && ls -la`, `PYTHONPATH=. python3 -m x`,
    `a | b` - so the string is split into segments, navigation and environment
    prefixes are dropped, and the phase is decided on what actually runs.
    """
    cmd = (command or "").strip()
    if not cmd:
        return "Execution"
    if _VERIFICATION_CMD.search(cmd):
        return "Verification"

    segments = []
    for raw in re.split(r"&&|\|\||;|\|", cmd):
        seg = re.sub(r"^\s*(\w+=\S*\s+)+", "", raw.strip())  # drop env assignments
        if not seg or re.match(r"^(cd|export|source|\.)\b", seg):
            continue
        segments.append(seg)
    if segments and all(_READONLY_CMD.match(s) for s in segments):
        return "Discovery"
    return "Execution"


def _tool_phase(name: str, command: str | None) -> str:
    n = (name or "").strip().lower()
    # MCP tools arrive as mcp__<server>__<tool>; classify on the trailing tool name.
    if n.startswith("mcp__"):
        n = n.split("__")[-1]

    # Every runtime names its shell differently - Bash, exec, local_shell - but a
    # shell call has to classify on the command it ran, or effort profiles from
    # two runtimes are not comparable.
    if n in ("bash", "shell", "run_command", "terminal", "exec", "exec_command",
             "local_shell", "local_shell_call", "shell_command"):
        return _shell_phase(command)
    if n in VERIFICATION_TOOLS:
        return "Verification"
    if n in EXECUTION_TOOLS:
        return "Execution"
    if n in DISCOVERY_TOOLS:
        return "Discovery"
    if n in PLANNING_TOOLS:
        return "Planning"
    # Unknown tool: treat any read-ish verb as discovery, otherwise as execution.
    if re.search(r"(^|_)(get|list|search|read|fetch|find|query|show)(_|$)", n):
        return "Discovery"
    return "Execution"


def classify_call(
    tool_calls: list[dict] | None,
    has_text: bool = False,
    has_thinking: bool = False,
) -> tuple[str, str]:
    """Return (observed_execution_phase, confidence) for one agent call.

    Confidence values:
      observed_high   - every tool in the call maps to the same phase
      observed_medium - the call mixed phases; resolved by precedence
      observed_low    - no tool signal at all; inferred from content type only
    """
    tool_calls = tool_calls or []
    phases = {_tool_phase(tc.get("name", ""), tc.get("command")) for tc in tool_calls}

    if not phases:
        if has_text:
            return "Reporting", "observed_low"
        if has_thinking:
            return "Planning", "observed_low"
        return "Unclassified", "observed_low"

    if len(phases) == 1:
        return phases.pop(), "observed_high"

    for phase in PHASE_PRECEDENCE:
        if phase in phases:
            return phase, "observed_medium"
    return "Unclassified", "observed_low"


def stage_for_timestamp(markers: list[dict], ts: str) -> tuple[str | None, str]:
    """Attribute a declared workflow stage to a call, from stage markers.

    `markers` is a time-ordered list of {stage, marked_at} records emitted by the
    agent during the run. A call belongs to the most recent marker at or before
    its start time.

    A marker with `closes: true` ends declared work rather than starting a stage;
    calls after it fall through to observed-phase attribution. Without one, the
    last stage has no closing boundary - every other stage is bounded by the next
    marker, but the final one runs to the end of the measurement window, so it
    collects whatever the agent did after the workflow finished: verifying files,
    writing its closing message, retrying a failed survey call. That is reported
    honestly as lower confidence rather than being silently attributed.

    Returns (stage, confidence) where confidence is:
      declared_explicit  - the call falls inside a closed declared stage window
      declared_open_ended - the call is after the final marker, which nothing closes
      unavailable        - no stage was declared covering this call
    """
    if not markers:
        return None, "unavailable"
    ordered = sorted(markers, key=lambda x: parse_ts(x["marked_at"]))
    call_at = parse_ts(ts)

    current: str | None = None
    current_at = None
    for m in ordered:
        if parse_ts(m["marked_at"]) > call_at:
            break
        if m.get("closes"):
            current, current_at = None, None
        else:
            current, current_at = m["stage"], parse_ts(m["marked_at"])
    if current is None:
        return None, "unavailable"

    bounded = any(
        parse_ts(m["marked_at"]) > current_at
        for m in ordered
        if m.get("closes") or m["stage"] != current
    )
    return current, "declared_explicit" if bounded else "declared_open_ended"


def open_ended_stage(markers: list[dict]) -> str | None:
    """The declared stage nothing closes, if there is one."""
    opening = [m for m in markers or [] if not m.get("closes")]
    if not opening:
        return None
    ordered = sorted(markers, key=lambda x: parse_ts(x["marked_at"]))
    last = ordered[-1]
    return None if last.get("closes") else last["stage"]
