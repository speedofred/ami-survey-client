"""MCP stdio server exposing the AMI survey to any MCP-capable agent.

Speaks JSON-RPC 2.0 over stdin/stdout (initialize, tools/list, tools/call).
Standard library only, so it runs with `python3 -m ami_survey.mcp_server` and
needs no install step.

Division of responsibility:
  * this server  - reads the runtime's own telemetry and talks to the API
  * the API      - owns the survey definition, validation and persistence
  * the skill    - tells the agent what order to call things in
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from . import client, config
from . import text as ami_text
from . import adapters
from .timeutil import normalize, parse_ts, utcnow

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ami-survey", "version": "0.1.0"}
_CURRENT_POINTER = config.RUNS_DIR / ".current_run"
_PENDING_STAGES = config.RUNS_DIR / ".pending_stages.json"


def log(msg: str) -> None:
    print(f"[ami-survey-mcp] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# current-run bookkeeping (so the agent rarely has to pass run_id)
# --------------------------------------------------------------------------- #

def _set_current(run_id: str) -> None:
    config.ensure_dirs()
    _CURRENT_POINTER.write_text(run_id)


def _clear_current() -> None:
    """A submitted run is no longer the active one - leaving the pointer in place
    makes every later tool call fail with a confusing 'already submitted'."""
    if _CURRENT_POINTER.exists():
        _CURRENT_POINTER.unlink()


def _current() -> str | None:
    if _CURRENT_POINTER.exists():
        return _CURRENT_POINTER.read_text().strip() or None
    return None


def _resolve_run(args: dict) -> str:
    run_id = args.get("run_id") or _current()
    if not run_id:
        raise ValueError(
            "No active survey run. Call ami_survey_begin first (or pass run_id)."
        )
    return run_id


# --------------------------------------------------------------------------- #
# stage markers emitted before the survey run exists
# --------------------------------------------------------------------------- #
# The measurement window closes at the human turn that asks for the survey, so
# ami_survey_begin is called AFTER the work is done - but stage markers have to
# be emitted DURING the work, when there is no run to attach them to. Markers
# emitted early are buffered here with the timestamp they were really emitted at,
# and adopted by the next ami_survey_begin. Nothing is invented after the fact:
# an adopted marker carries the same marked_at it was recorded with.

def _load_pending() -> list[dict]:
    if not _PENDING_STAGES.exists():
        return []
    try:
        data = json.loads(_PENDING_STAGES.read_text())
    except (json.JSONDecodeError, OSError):
        log("pending stage buffer was unreadable; starting a fresh one")
        return []
    markers = data.get("markers") if isinstance(data, dict) else data
    return markers if isinstance(markers, list) else []


def _save_pending(markers: list[dict]) -> None:
    config.ensure_dirs()
    _PENDING_STAGES.write_text(json.dumps({"markers": markers}, indent=2))


def _buffer_marker(args: dict, reason: str) -> dict:
    # Normalised here, with the same rule the API applies, so what is echoed to
    # the agent is what will be stored. Echoing the raw text instead told an
    # agent its marker read "Score &amp; Tier Candidates"; it concluded it had
    # corrupted the name and sent a corrective duplicate, which is in the record.
    stage = ami_text.normalise(args.get("stage"))
    if not stage and not args.get("closes"):
        raise ValueError("stage is required.")
    stage = stage or "(declared stages complete)"
    marker = {
        "stage": stage,
        "closes": bool(args.get("closes")),
        "marked_at": normalize(args.get("marked_at")) or utcnow(),
        "note": (args.get("note") or "").strip() or None,
        "cwd": args.get("cwd") or os.getcwd(),
        "session_id": args.get("session_id"),
        "buffered_because": reason,
    }
    pending = _load_pending()
    pending.append(marker)
    pending.sort(key=lambda m: parse_ts(m["marked_at"]))
    _save_pending(pending)
    return {
        "stage": stage,
        "marked_at": marker["marked_at"],
        "status": "buffered",
        "buffered_because": reason,
        "pending_markers": [
            {"stage": m["stage"], "marked_at": m["marked_at"]} for m in pending
        ],
        "instruction": (
            "Marker recorded locally at the moment you emitted it. The next "
            "ami_survey_begin in this workspace attaches it to the new run with "
            "this exact timestamp, so keep marking stages as you work and open "
            "the survey after the work is finished."
        ),
    }


def _within(ts: str, start: str | None, end: str | None) -> bool:
    at = parse_ts(ts)
    if start and at < parse_ts(start):
        return False
    if end and at > parse_ts(end):
        return False
    return True


def _adopt_pending(run_id: str, cwd: str, window_start: str | None) -> dict:
    """Attach buffered markers for this workspace to a freshly opened run."""
    pending = _load_pending()
    if not pending:
        return {"adopted": [], "kept_for_other_workspaces": 0, "discarded": []}

    now = utcnow()
    adopted, discarded, kept = [], [], []
    for m in pending:
        if m.get("cwd") and m["cwd"] != cwd:
            kept.append(m)
            continue
        if not _within(m["marked_at"], window_start, now):
            discarded.append(
                {
                    "stage": m["stage"],
                    "marked_at": m["marked_at"],
                    "reason": "emitted outside this run's measurement window",
                }
            )
            continue
        try:
            client.post(
                f"/runs/{run_id}/stages",
                {
                    "stage": m["stage"],
                    "closes": bool(m.get("closes")),
                    "marked_at": m["marked_at"],
                    "note": m.get("note"),
                    "recorded_before_run_opened": True,
                },
            )
        except client.ApiCallFailed as exc:
            kept.append(m)
            discarded.append(
                {"stage": m["stage"], "marked_at": m["marked_at"], "reason": str(exc)}
            )
            continue
        adopted.append({"stage": m["stage"], "marked_at": m["marked_at"]})

    _save_pending(kept)
    return {
        "adopted": adopted,
        "kept_for_other_workspaces": sum(1 for m in kept if m.get("cwd") != cwd),
        "discarded": discarded,
    }


def _drop_pending_up_to(cwd: str, end: str | None) -> None:
    """After a submit, markers for work already surveyed must not leak into the
    next run of the same session (whose window starts at the first human turn)."""
    if not end:
        return
    remaining = [
        m
        for m in _load_pending()
        if m.get("cwd") != cwd or parse_ts(m["marked_at"]) > parse_ts(end)
    ]
    _save_pending(remaining)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

def t_get_survey(_args: dict) -> dict:
    return client.get("/survey")


def t_get_instructions(args: dict) -> str:
    """The procedure, for a client whose harness has no skill mechanism."""
    runtime = args.get("runtime") or "mcp"
    return client.get(f"/instructions/{runtime}")


def t_get_grading_scale(_args: dict) -> dict:
    return client.get("/survey/grading-scale")


def t_get_workflow_categories(_args: dict) -> dict:
    return client.get("/survey/workflow-categories")


def t_get_scorecard(args: dict) -> dict:
    return client.get(f"/runs/{_resolve_run(args)}/scorecard")


def t_write_findings(args: dict) -> dict:
    body = {k: v for k, v in args.items() if k not in ("run_id",)}
    return client.post(f"/runs/{_resolve_run(args)}/narrative", body)


def t_survey_begin(args: dict) -> dict:
    name = (args.get("workflow_name") or "").strip()
    desc = (args.get("workflow_description") or "").strip()
    cwd = args.get("cwd") or os.getcwd()

    probe: dict[str, Any] = {}
    probe_error = None
    try:
        probe = adapters.probe(
            cwd=cwd,
            session_id=args.get("session_id"),
            transcript_path=args.get("transcript_path"),
            adapter=args.get("adapter"),
        )
    except adapters.TelemetryNotFound as exc:
        probe_error = str(exc)

    window = probe.get("suggested_window", {})
    payload = {
        "workflow_name": name,
        "workflow_description": desc,
        "workflow_category": args.get("workflow_category"),
        "work_unit": args.get("work_unit"),
        "work_unit_count": args.get("work_unit_count"),
        "workflow_start_time": args.get("workflow_start_time") or window.get("start"),
        "workflow_end_time": args.get("workflow_end_time") or window.get("end"),
        "workflow_start_time_basis": (
            "supplied by the agent"
            if args.get("workflow_start_time")
            else window.get("start_basis")
        ),
        "workflow_end_time_basis": (
            "supplied by the agent"
            if args.get("workflow_end_time")
            else window.get("end_basis")
        ),
        "telemetry_adapter": probe.get("adapter"),
        "runtime_metadata": probe.get("runtime_metadata", {}),
    }
    created = client.post("/runs", payload)
    _set_current(created["run_id"])
    stages = _adopt_pending(
        created["run_id"], cwd, payload["workflow_start_time"]
    )

    return {
        **created,
        "runtime_detected": probe.get("runtime_metadata"),
        "measurement_window": window,
        "calls_visible_in_session": probe.get("calls_in_session"),
        "telemetry_probe_error": probe_error,
        "stage_markers": stages,
        "instruction": (
            "Next: call ami_collect_telemetry to measure this run, then "
            "ami_get_grading_scale, then ami_submit_survey with your graded output."
        ),
    }


def t_mark_stage(args: dict) -> dict:
    """Attach a stage marker to the open run, or buffer it until one exists.

    Markers are emitted while the work happens; the run is opened afterwards, so
    'no open run yet' is the normal case, not an error.
    """
    run_id = args.get("run_id") or _current()
    if not run_id:
        return _buffer_marker(args, "no survey run was open when the stage was entered")
    try:
        result = client.post(
            f"/runs/{run_id}/stages",
            {
                "stage": args.get("stage"),
                "closes": bool(args.get("closes")),
                "marked_at": args.get("marked_at"),
                "note": args.get("note"),
            },
        )
    except client.ApiCallFailed as exc:
        if exc.status in (404, 409):
            reason = (
                f"run {run_id} is already submitted"
                if exc.status == 409
                else f"run {run_id} no longer exists"
            )
            if run_id == _current():
                _clear_current()
            return _buffer_marker(args, reason)
        raise
    return {**result, "status": "attached"}


def t_collect_telemetry(args: dict) -> dict:
    run_id = _resolve_run(args)
    run = client.get(f"/runs/{run_id}")

    window_start = args.get("window_start") or run.get("workflow_start_time")
    window_end = (
        args.get("window_end") or run.get("workflow_end_time") or run.get("survey_started_at")
    )

    telemetry = adapters.collect(
        cwd=args.get("cwd") or os.getcwd(),
        session_id=args.get("session_id"),
        transcript_path=args.get("transcript_path"),
        window_start=window_start,
        window_end=window_end,
        adapter=args.get("adapter"),
    )

    result = client.post(
        f"/runs/{run_id}/calls",
        {
            "calls": telemetry["calls"],
            "adapter": telemetry["adapter"],
            "runtime_metadata": telemetry["runtime_metadata"],
            "default_workflow_start_time": telemetry["default_workflow_start_time"],
            "measurement_window": {"start": window_start, "end": window_end},
            "replace": True,
        },
    )
    preview = client.get(f"/runs/{run_id}/preview")
    return {
        "run_id": run_id,
        "measured": {
            "transcript": telemetry["runtime_metadata"]["transcript_path"],
            "calls_in_window": len(telemetry["calls"]),
            "calls_in_session": telemetry["calls_in_session"],
            "window": {"start": window_start, "end": window_end},
        },
        "totals": {k: v for k, v in result.items() if k != "completeness"},
        "collected_fields": preview["fields"],
        "agent_effort_profile": preview["agent_effort_profile"],
        "pricing_resolution": preview["pricing_resolution"],
        "completeness": preview["completeness"],
        "instruction": (
            "These values are measured from the runtime's own records - report them "
            "as collected; do not substitute your own estimates."
        ),
    }


def t_record_calls(args: dict) -> dict:
    """Telemetry entry point for runtimes other than Claude Code."""
    run_id = _resolve_run(args)
    result = client.post(
        f"/runs/{run_id}/calls",
        {
            "calls": args.get("calls") or [],
            "adapter": args.get("adapter") or "external",
            "runtime_metadata": args.get("runtime_metadata") or {},
            "replace": args.get("replace", True),
        },
    )
    preview = client.get(f"/runs/{run_id}/preview")
    return {"run_id": run_id, "totals": result, "collected_fields": preview["fields"]}


def t_survey_status(args: dict) -> dict:
    run_id = _resolve_run(args)
    return client.get(f"/runs/{run_id}/preview")


def t_submit_survey(args: dict) -> dict:
    run_id = _resolve_run(args)
    body = {
        "agent_output_grade": args.get("agent_output_grade"),
        "grade_justification": args.get("grade_justification"),
        "grade_evidence": args.get("grade_evidence"),
        "grader": args.get("grader", "self"),
        "workflow_name": args.get("workflow_name"),
        "workflow_description": args.get("workflow_description"),
        "workflow_end_time": args.get("workflow_end_time"),
        "allow_empty_telemetry": args.get("allow_empty_telemetry", False),
    }
    result = client.post(f"/runs/{run_id}/submit", body)
    # A hosted server returns endpoints rather than its own filesystem. Join them
    # to the address we called, so what the agent shows the human is fetchable.
    saved = result.get("saved") or {}
    if any(str(v).startswith("/") and not str(v).startswith("//") for v in saved.values()):
        base = config.API_URL.rstrip("/")
        result["saved"] = {
            k: (f"{base}{v}" if str(v).startswith("/") else v) for k, v in saved.items()
        }
    _clear_current()
    _drop_pending_up_to(
        args.get("cwd") or os.getcwd(),
        (result.get("fields") or {}).get("workflow_end_time"),
    )
    return {
        **result,
        "instruction": (
            "Survey persisted. Report the saved links and the headline numbers to "
            "the human exactly as returned - including the ?key=... on each link, "
            "which is what lets them open their own report in a browser. Report "
            "any warnings too; they are limits on how far the numbers can be read."
        ),
    }


def t_get_report(args: dict) -> str:
    run_id = args.get("run_id") or _current()
    if not run_id:
        return client.get("/responses/index.md")
    # The .md form: /report now renders a page for a human to read, and an
    # agent asking for the report wants the report.
    return client.get(f"/runs/{run_id}/report.md")


def t_list_surveys(_args: dict) -> dict:
    return {
        "responses": client.get("/responses")["responses"],
        "index_markdown": f"{config.API_URL}/responses/index.md",
        "index_csv": str(config.RESPONSES_DIR / "index.csv"),
    }


TOOLS: list[dict] = [
    {
        "name": "ami_survey_begin",
        "description": (
            "Open an AMI survey for the workflow you have just completed. Detects the "
            "runtime, session and measurement window automatically, and adopts any "
            "stage markers you buffered with ami_mark_stage while working. Call this "
            "FIRST, after the workflow's real work is finished, so the survey's own "
            "token spend is excluded from the measurements."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Short reusable label for the workflow, e.g. "
                                   "'Support Ticket Triage & Response'.",
                },
                "workflow_description": {
                    "type": "string",
                    "description": "1-3 sentences: what the workflow was given and what "
                                   "business work it produced. Minimum 20 characters.",
                },
                "workflow_category": {
                    "type": "string",
                    "description": "Optional. The workflow's category, from ami_get_workflow_categories. Decides which other workflows this run is compared against; an undeclared workflow is only ever compared against itself. Take it from the workflow's own workflow.json where one exists - do not invent one.",
                },
                "work_unit": {
                    "type": "string",
                    "description": "Optional, but declare it together with work_unit_count. The countable thing this workflow handled: 'ticket', 'CV', 'support email'. It is what cost and duration get divided by, so a workflow that did more work is not penalised for costing more.",
                },
                "work_unit_count": {
                    "type": "integer",
                    "description": 'Optional, but declare it together with work_unit. How many work units this run actually handled - a whole number you can point at in the output, not an estimate.',
                },
                "workflow_start_time": {
                    "type": "string",
                    "description": "Optional ISO-8601 override for when the workflow began. "
                                   "Defaults to the first human turn of this session.",
                },
                "workflow_end_time": {
                    "type": "string",
                    "description": "Optional ISO-8601 override for when the workflow ended.",
                },
                "session_id": {"type": "string", "description": "Optional session id override."},
                "transcript_path": {
                    "type": "string",
                    "description": "Optional explicit transcript path override.",
                },
                "cwd": {"type": "string", "description": "Optional working-directory override."},
                "adapter": {
                    "type": "string",
                    "description": "Force a telemetry adapter instead of detecting the "
                                   "runtime. Rarely needed.",
                },
            },
            "required": ["workflow_name", "workflow_description"],
        },
        "handler": t_survey_begin,
    },
    {
        "name": "ami_collect_telemetry",
        "description": (
            "Measure the workflow run: detects which agent runtime you are in, reads "
            "that runtime's own call records (token usage, model, timings reported by "
            "the provider), attributes each call to a stage or observed execution "
            "phase, and stores the result on the run. Returns the collected inventory "
            "fields. Use these numbers verbatim. If your runtime has no adapter, this "
            "says so and you should use ami_record_calls instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Defaults to the active run."},
                "window_start": {"type": "string", "description": "ISO-8601 override."},
                "window_end": {"type": "string", "description": "ISO-8601 override."},
                "session_id": {"type": "string"},
                "transcript_path": {"type": "string"},
                "cwd": {"type": "string"},
                "adapter": {
                    "type": "string",
                    "description": "Force a telemetry adapter instead of detecting the "
                                   "runtime. Rarely needed.",
                },
            },
        },
        "handler": t_collect_telemetry,
    },
    {
        "name": "ami_record_calls",
        "description": (
            "Telemetry entry point for agents NOT running in Claude Code. Post the "
            "provider-reported usage for each API call the workflow made. Every record "
            "needs model, start_time, end_time, input_tokens, output_tokens - values "
            "read from real API responses, never estimated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "adapter": {
                    "type": "string",
                    "description": "Name of the runtime/SDK the records came from.",
                },
                "calls": {
                    "type": "array",
                    "description": "Call records.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "call_id": {"type": "string"},
                            "model": {"type": "string"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "input_tokens": {"type": "integer"},
                            "output_tokens": {"type": "integer"},
                            "workflow_stage": {"type": "string"},
                            "tool_calls": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "command": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "required": [
                            "model", "start_time", "end_time",
                            "input_tokens", "output_tokens",
                        ],
                    },
                },
                "runtime_metadata": {
                    "type": "object",
                    "description": "e.g. {platform, runtime, version}.",
                },
                "replace": {"type": "boolean"},
            },
            "required": ["calls"],
        },
        "handler": t_record_calls,
    },
    {
        "name": "ami_mark_stage",
        "description": (
            "Declare the workflow stage you are entering, e.g. 'Classify Severity' or "
            "'Draft Customer Reply'. Call it as you move through the workflow to get a "
            "declared-stage Agent Effort Profile; without markers the profile falls "
            "back to AMI-observed execution phases. No survey run is needed first: "
            "markers emitted before ami_survey_begin are buffered with the timestamp "
            "you emitted them at and attached to the run when it opens. "
            "When the last stage is done, call once more with closes=true - each "
            "marker ends the stage before it, so without a closing one the final "
            "stage runs to the end of the measurement window and absorbs everything "
            "you do afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "description": "Name of the stage being entered."},
                "closes": {
                    "type": "boolean",
                    "description": "True to end declared work rather than start a stage. "
                                   "Call this when the final stage is complete, before "
                                   "you verify output or report back; work after it is "
                                   "attributed to observed phases instead of to a stage.",
                },
                "run_id": {"type": "string"},
                "marked_at": {"type": "string", "description": "ISO-8601; defaults to now."},
                "note": {"type": "string"},
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace override; buffered markers are "
                                   "adopted by a run opened in the same workspace.",
                },
            },
        },
        "handler": t_mark_stage,
    },
    {
        "name": "ami_get_grading_scale",
        "description": (
            "Return the AMI output-quality grading scale. Read this before grading: "
            "agent_output_grade must be one of its codes and is validated on submit."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_get_grading_scale,
    },
    {
        "name": "ami_get_workflow_categories",
        "description": (
            'The workflow categories a run may declare itself into, and what each one covers. A category decides which other workflows this run is compared against, so read the list rather than inventing a label.'
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_get_workflow_categories,
    },
    {
        "name": "ami_get_scorecard",
        "description": (
            "The scorecard for a submitted run: the AMI Maturity Index, the Performance Score, the five pillars, and structured findings. Every number and finding is computed from the run's own data - the server calls no model. If a human wants this read back as prose, write it yourself from narration_brief.findings, following the instructions there."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string",
                            "description": "Defaults to the run you just submitted."},
            },
        },
        "handler": t_get_scorecard,
    },
    {
        "name": "ami_write_findings",
        "description": (
            "Write the judgement sections of a run's scorecard. The server computes every number and the three sections that follow from them; these four are readings of the work that no arithmetic produces, so they are yours to write. Call ami_get_scorecard first and use narration_brief.sections_awaiting_you - it carries the brief for each. Ground every sentence in the run's own evidence; do not invent industry context you were not given."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {'type': 'string', 'description': 'The submitted run.'},
                "workflow_opportunity": {'type': 'string', 'description': 'The single biggest improvement available to this workflow. Name the stage.'},
                "workflow_next_step": {'type': 'string', 'description': 'One concrete change, specific enough to act on this week.'},
                "industry_opportunity": {'type': 'string', 'description': 'What this workflow being agent-run means commercially. Say so plainly if you were given no industry context.'},
                "industry_next_step": {'type': 'string', 'description': 'One thing the business should decide or standardise.'},
                "key_finding": {'type': 'string', 'description': 'Optional. Overrides the derived summary if you have read the output and know better than the arithmetic does.'},
            },
            "required": ["run_id"],
        },
        "handler": t_write_findings,
    },
    {
        "name": "ami_get_instructions",
        "description": (
            "Return the AMI survey procedure as markdown. Call this FIRST if your "
            "harness has no skill mechanism that already gave you the procedure - it "
            "tells you the call order and, for your runtime, how telemetry is obtained. "
            "Claude Code agents already have it as a skill and do not need this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "runtime": {
                    "type": "string",
                    "enum": ["mcp", "claude-code", "http"],
                    "description": "Your runtime. 'mcp' (default) for any MCP client "
                                   "that is not Claude Code.",
                }
            },
        },
        "handler": t_get_instructions,
    },
    {
        "name": "ami_get_survey",
        "description": (
            "Return the survey definition: every field from Collection_Inventory.csv, "
            "how each one is obtained, and which ones you must answer yourself."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_get_survey,
    },
    {
        "name": "ami_survey_status",
        "description": (
            "Show the current values collected for the active run, which inventory "
            "fields are still empty, and what is blocking submission."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
        },
        "handler": t_survey_status,
    },
    {
        "name": "ami_submit_survey",
        "description": (
            "Submit and persist the survey. Requires a grade from the AMI grading "
            "scale, a justification, and evidence (the concrete artifacts produced). "
            "Writes JSON + Markdown + a CSV index row to disk and returns the paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_output_grade": {
                    "type": "string",
                    "description": "A grade code from ami_get_grading_scale.",
                },
                "grade_justification": {
                    "type": "string",
                    "description": "Why that grade, measured against the workflow's stated "
                                   "requirements. At least 40 characters.",
                },
                "grade_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete artifacts being graded: file paths, ticket ids, "
                                   "message ids, tool outputs.",
                },
                "grader": {
                    "type": "string",
                    "description": "'self' when the agent grades its own output, 'human' or "
                                   "'external_reviewer' when a person supplied the grade.",
                },
                "run_id": {"type": "string"},
                "workflow_name": {"type": "string"},
                "workflow_description": {"type": "string"},
                "workflow_end_time": {"type": "string"},
                "allow_empty_telemetry": {
                    "type": "boolean",
                    "description": "Only for deliberately unmeasured runs; measurement fields "
                                   "will be null.",
                },
            },
            "required": ["agent_output_grade", "grade_justification", "grade_evidence"],
        },
        "handler": t_submit_survey,
    },
    {
        "name": "ami_get_report",
        "description": (
            "Render the human-readable Markdown report for a run (defaults to the "
            "active run). With no run and no active run, returns the index of all "
            "submitted surveys."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
        },
        "handler": t_get_report,
    },
    {
        "name": "ami_list_surveys",
        "description": "List all submitted survey responses and where they are stored.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_list_surveys,
    },
]

HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing
# --------------------------------------------------------------------------- #

def _call_tool(name: str, arguments: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    try:
        result = handler(arguments or {})
    except client.ApiCallFailed as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {"error": str(exc.payload)}
        # An agent that is only told "Unrecognised token" will try again, and
        # again, because retrying is what a transient failure deserves. This one
        # is not transient: nothing the agent can do will make the same token
        # valid, so say so in the response rather than leaving it to be inferred.
        if exc.status in (401, 403):
            payload = {
                **payload,
                "terminal": True,
                "retrying_will_not_help": (
                    "This is an authentication failure, not a transient error. "
                    "The same request will fail identically every time."
                ),
                "what_to_do": (
                    "Stop, and tell the human their survey token is missing, "
                    "revoked or wrong - it is configured on their machine and "
                    "only they can change it. Do not retry, and do not fall back "
                    "to reporting the numbers yourself: an unsubmitted survey is "
                    "recoverable, an invented one is not."
                ),
            }
        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001 - report failure to the agent, keep serving
        log(traceback.format_exc())
        return {
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            "isError": True,
        }
    text = result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "AMI workflow survey. After finishing a workflow: ami_survey_begin -> "
                    "ami_collect_telemetry -> ami_get_grading_scale -> ami_submit_survey. "
                    "All measurement fields come from the runtime's own records; never "
                    "estimate them."
                ),
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOL_SPECS}}
    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": _call_tool(params.get("name", ""), params.get("arguments") or {}),
        }
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
    if mid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    config.ensure_dirs()
    log(f"ready - api={config.API_URL} data={config.DATA_DIR}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"skipping non-JSON input: {line[:120]}")
            continue
        try:
            response = handle(message)
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": "Internal error"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
