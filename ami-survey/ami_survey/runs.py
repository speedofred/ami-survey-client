"""The run, as a thing that happens rather than as a request being served.

Everything here works on a plain run dict: opening one, recording what the agent
did, and closing it into a finished submission. No HTTP, no authorisation, no
knowledge of who is asking - those belong to whoever calls this.

That split is what lets the same code run in two places that must not drift
apart. The hosted API wraps these with tokens, ownership and quotas, because it
serves strangers. The collector on a tester's own machine calls them directly and
builds its submission locally, so the call records - which carry prompts and
shell commands - never leave that machine unsealed.

`RunError` carries an HTTP status, which is a small concession: this module has
no business knowing about HTTP, but every caller reports failures as one, and a
symbolic code plus a translation table in each caller would be more to get wrong
than it saves.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from . import categories, compute, config, grading, inventory, storage
from . import text as ami_text
from .timeutil import min_ts, normalize, parse_ts, utcnow


class RunError(Exception):
    """A run could not be advanced, and why - in the caller's own terms."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


REQUIRED_CALL_KEYS = {"model", "start_time", "end_time", "input_tokens", "output_tokens"}


# Longest each field may be once normalised. Unbounded text from a stranger ends
# up in a Markdown report and a CSV row, so it is capped at the boundary.
MAX_TEXT = {
    "workflow_name": 200,
    "workflow_description": 2000,
    "stage": 200,
    "default": 4000,
}


def clean_text(raw: str | None, field: str = "default") -> str:
    """Trim, undo transport HTML-escaping, and make the value safe to store.

    Once submissions can come from strangers, these strings reach a Markdown
    report and a CSV. Control characters are removed and length is capped here,
    at the boundary; escaping for a particular output format is the renderer's
    job, because what is dangerous depends on where the value is going.
    """
    # Normalisation is shared with the client, which has to echo a buffered
    # stage marker back to the agent before this handler ever sees it.
    text = ami_text.normalise(raw)
    if config.PUBLIC_MODE:
        text = scrub_local_paths(text)
    limit = MAX_TEXT.get(field, MAX_TEXT["default"])
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


#: call-record fields that describe the submitter's machine rather than the run
_MACHINE_DETAIL = ("evidence", "transcript_path", "cwd", "session_id")


#: runtime_metadata worth keeping - what ran, not where it ran or who ran it
_RUNTIME_KEEP = (
    "platform", "runtime", "os", "entrypoint", "version", "model_provider",
    "model_name", "usage_mapping", "pricing_provider_hint", "is_subagent",
)


#: a home directory prefix, which carries the submitter's account name.
#: Windows spells it C:\Users\name\ and accepts either separator.
_HOME_PREFIX = re.compile(
    r"(?:/Users|/home)/[^/\s\"']+/"
    r"|[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+[\\/]"
)


#: a drive-letter root, which makes a Windows path absolute
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:/")


def scrub_local_paths(text: str) -> str:
    """Replace a home directory prefix with `~/`.

    For prose - a grade justification, a workflow description - where a path may
    appear mid-sentence. Only the identifying part is removed; the rest stays
    readable.
    """
    return _HOME_PREFIX.sub("~/", text or "")


def redact_evidence(item) -> str:
    """Reduce a cited artifact to something that identifies it, not its machine.

    `grade_evidence` names the files an agent graded, and agents cite them by
    absolute path: an 18-call run arrived quoting
    `/Users/<name>/Development/<client>/.../output/triage.json` seven times over.
    The last two segments say exactly as much to an analyst - `output/triage.json`
    - without the account name or the directory tree above it, which can carry a
    client or project name. Anything that is not a path (a ticket id, a message
    id) is left alone.
    """
    text = scrub_local_paths(str(item)).strip()
    # Compare on forward slashes so one rule covers both separators.
    unified = text.replace("\\", "/")
    if not (unified.startswith(("/", "~/")) or _DRIVE_ROOT.match(unified)):
        return text
    parts = [
        p for p in unified.replace("~/", "", 1).split("/")
        if p and not re.fullmatch(r"[A-Za-z]:", p)
    ]
    return "/".join(parts[-2:]) if parts else text


def redact_runtime_metadata(meta: dict) -> dict:
    """Keep the runtime's identity, drop the submitter's filesystem and session.

    `runtime_metadata` carries `transcript_path` - an absolute path containing
    the submitter's username - plus their session id and git branch. The survey
    needs to know it was Codex 0.146 on macOS; it does not need to know whose
    machine that was.
    """
    if not isinstance(meta, dict):
        return {}
    out = {k: v for k, v in meta.items() if k in _RUNTIME_KEEP}
    out["redacted"] = True
    return out


def redact_call(call: dict) -> dict:
    """Strip everything that describes the submitter's machine.

    A call record carries the shell command behind each tool call and the path to
    the session log. On a server collecting other people's runs, those are their
    secrets, their internal hostnames and their username - none of which the
    survey measures. Tool *names* are kept, because the phase classifier needs
    them; the command is not, so the classification becomes name-only and says so.
    """
    out = {k: v for k, v in call.items() if k not in _MACHINE_DETAIL}
    tools = []
    for tool in call.get("tool_calls") or []:
        if isinstance(tool, dict):
            tools.append({"name": tool.get("name", "")})
    out["tool_calls"] = tools
    # Adapters build call ids from the runtime's session id, which correlates
    # every submission from one session. Hashing keeps deduplication working -
    # the same call still maps to the same id - without storing the correlator.
    if out.get("call_id"):
        out["call_id"] = "c-" + hashlib.sha256(
            str(out["call_id"]).encode()
        ).hexdigest()[:16]
    out["redacted"] = True
    return out


def _incoming_runtime_metadata(meta) -> dict:
    """Runtime metadata as it should be stored, given the server's mode."""
    meta = meta or {}
    return redact_runtime_metadata(meta) if config.PUBLIC_MODE else meta


def _require_open(run: dict, what: str) -> None:
    if run.get("status") != "open":
        raise RunError(409, f"Run {run['run_id']} is already submitted, so {what}.")


def mark_stage(
    run: dict,
    *,
    stage: str | None = None,
    closes: bool = False,
    marked_at: str | None = None,
    note: str | None = None,
    recorded_before_run_opened: bool = False,
    marked_at_claimed: str | None = None,
    marked_at_source: str | None = None,
) -> list[dict]:
    """Attach one stage marker and keep the markers in time order."""
    if run.get("status") != "open":
        raise RunError(
            409,
            f"Run {run['run_id']} is already submitted, so no further stage markers "
            "can be attached to it. Markers for work done since then belong to the "
            "next run: keep emitting them (ami_mark_stage buffers them locally) and "
            "they will attach when ami_survey_begin opens that run.",
        )
    # Through clean_text like every other agent-supplied string. A transport that
    # HTML-escapes tool arguments turns "Score & Tier" into "Score &amp; Tier",
    # and because the effort profile groups by stage name, one stage silently
    # becomes two rows with the work split between them.
    closes = bool(closes)
    stage = clean_text(stage, "stage")
    if not stage and not closes:
        raise RunError(400, "stage is required.")
    marker = {
        # A closing marker ends declared work rather than starting a stage. It
        # keeps a stage name only for the record; nothing is attributed to it.
        "stage": stage or "(declared stages complete)",
        "closes": closes,
        "marked_at": normalize(marked_at) or utcnow(),
        "note": clean_text(note) or None,
        # True when the marker was emitted during the work, before this run was
        # opened, and buffered until it existed. The timestamp is still the real
        # emission time - it is not a retroactive guess.
        "recorded_before_run_opened": bool(recorded_before_run_opened),
        # How the timestamp was arrived at, so a reader can tell a time the
        # server watched arrive from one the agent typed.
        "marked_at_claimed": normalize(marked_at_claimed) or None,
        "marked_at_source": clean_text(marked_at_source) or (
            "supplied by the agent" if marked_at else "defaulted to now"
        ),
    }
    run["stage_markers"].append(marker)
    run["stage_markers"].sort(key=lambda x: parse_ts(x["marked_at"]))
    storage.save_run(run)
    return run["stage_markers"]


def set_answers(
    run: dict,
    *,
    workflow_name: str | None = None,
    workflow_description: str | None = None,
    workflow_start_time: str | None = None,
    workflow_end_time: str | None = None,
) -> dict:
    """Correct what the run says about itself, before it is closed."""
    _require_open(run, "its answers can no longer be changed")
    supplied = {"workflow_name": workflow_name, "workflow_description": workflow_description}
    for key, value in supplied.items():
        if value:
            run["answers"][key] = clean_text(value, key)
    times = {"workflow_start_time": workflow_start_time, "workflow_end_time": workflow_end_time}
    for key, value in times.items():
        if value:
            run[key] = normalize(value)
            run["workflow_time_source"][key] = "explicitly supplied by the agent"
    storage.save_run(run)
    return run["answers"]


def completeness(run: dict) -> dict:
    """Which inventory fields currently have a value, and what is blocking the rest."""
    response = compute.build_response(run)
    fields = response["fields"]
    missing = [name for name, value in fields.items() if value in (None, "")]
    blockers = []
    if not run.get("calls"):
        blockers.append(
            "No telemetry recorded. Run ami_collect_telemetry (Claude Code) or POST "
            "/runs/{run_id}/calls with the provider-reported usage for each call."
        )
    if not response["pricing_resolution"].get("resolved"):
        blockers.append(
            f"Pricing unresolved for model "
            f"{response['pricing_resolution'].get('model_name')!r}. Add an entry to "
            f"{config.PRICING_OVERRIDES_FILE.name} or refresh the LiteLLM price map."
        )
    if run.get("grading") is None:
        blockers.append("Not graded yet - agent_output_grade is set at submission.")
    return {
        "fields_total": len(inventory.RUN_FIELDS),
        "fields_populated": len(inventory.RUN_FIELDS) - len(missing),
        "missing_fields": missing,
        "blockers": blockers,
        "effort_profile_rows": len(response["agent_effort_profile"]),
    }


def preview(run: dict) -> dict:
    """What the submission would say if it were closed now."""
    response = compute.build_response(run)
    return {
        "run_id": run["run_id"],
        "fields": response["fields"],
        "agent_effort_profile": response["agent_effort_profile"],
        "derived_analysis": response["derived_analysis"],
        "pricing_resolution": response["pricing_resolution"],
        "completeness": completeness(run),
    }


def record_calls(
    run: dict,
    *,
    calls: list,
    adapter: str | None = None,
    runtime_metadata: dict | None = None,
    replace: bool = True,
    default_workflow_start_time: str | None = None,
    measurement_window: dict | None = None,
) -> dict:
    """Attach the call records an adapter read, and return the running totals."""
    _require_open(run, "no further calls can be recorded against it")

    if not isinstance(calls, list):
        raise RunError(400, "calls must be a list of call records.")
    if len(calls) > config.MAX_CALLS_PER_RUN:
        raise RunError(
            413,
            f"{len(calls)} call records exceeds the {config.MAX_CALLS_PER_RUN} "
            "per-run limit.",
        )

    cleaned = []
    for i, c in enumerate(calls):
        if not isinstance(c, dict):
            raise RunError(400, f"calls[{i}] must be an object.")
        missing = REQUIRED_CALL_KEYS - set(c)
        if missing:
            raise RunError(
                400,
                f"calls[{i}] is missing required measured keys: {sorted(missing)}. "
                "Every call record must carry the provider-reported model, timing and "
                "token counts - the survey does not accept estimated telemetry.",
            )
        c = dict(c)
        c.setdefault("call_id", f"call-{i}")
        c["start_time"] = normalize(c["start_time"])
        c["end_time"] = normalize(c["end_time"])
        if c.get("duration_seconds") is None:
            c["duration_seconds"] = round(
                (parse_ts(c["end_time"]) - parse_ts(c["start_time"])).total_seconds(), 3
            )
        c.setdefault("source", adapter or "external")
        cleaned.append(redact_call(c) if config.PUBLIC_MODE else c)

    if replace:
        run["calls"] = cleaned
    else:
        known = {c.get("call_id") for c in run["calls"]}
        run["calls"].extend(c for c in cleaned if c.get("call_id") not in known)

    if runtime_metadata:
        run["runtime_metadata"] = {
            **run["runtime_metadata"],
            **_incoming_runtime_metadata(runtime_metadata),
        }
    if adapter:
        run["telemetry_adapter"] = adapter

    # A workflow start time was not supplied: fall back to the adapter's observed
    # start of the session, else the first observed agent call.
    if not run.get("workflow_start_time"):
        if default_workflow_start_time:
            run["workflow_start_time"] = normalize(default_workflow_start_time)
            run["workflow_time_source"]["workflow_start_time"] = (
                "first user turn of the session, observed by the telemetry adapter"
            )
        elif cleaned:
            run["workflow_start_time"] = min_ts([c["start_time"] for c in cleaned])
            run["workflow_time_source"]["workflow_start_time"] = (
                "earliest observed agent call (no explicit workflow marker)"
            )

    if measurement_window:
        run["measurement_window"] = measurement_window

    storage.save_run(run)
    return {
        "calls_recorded": len(run["calls"]),
        "input_tokens": sum(int(c.get("input_tokens") or 0) for c in run["calls"]),
        "output_tokens": sum(int(c.get("output_tokens") or 0) for c in run["calls"]),
    }


def new_run(
    *,
    workflow_name: str | None,
    workflow_description: str | None,
    workflow_category=None,
    work_unit=None,
    work_unit_count=None,
    workflow_start_time: str | None = None,
    workflow_end_time: str | None = None,
    workflow_start_time_basis: str | None = None,
    workflow_end_time_basis: str | None = None,
    telemetry_adapter: str | None = None,
    runtime_metadata: dict | None = None,
    owner: str | None = None,
    agent_identity=None,
    corroborated: bool = True,
) -> tuple[dict, dict]:
    """Open a run. Returns it, and the declaration it resolved to.

    Identity is passed in rather than looked up: on the hosted API it comes from
    the token, and on a collector there is nobody to be. Either way it is the
    caller's to establish - this module has no way to find out who is asking, and
    should not.
    """
    name = clean_text(workflow_name, "workflow_name")
    desc = clean_text(workflow_description, "workflow_description")
    if not name:
        raise RunError(400, "workflow_name is required to open a survey run.")
    if len(desc) < 20:
        raise RunError(
            400,
            "workflow_description is required and must be at least 20 characters: "
            "state what the workflow was given and what it produced.",
        )

    # What the workflow says about itself: which workflows it may be compared
    # against, and what its cost gets divided by. Rejected at the boundary
    # rather than stored and worked around later - a category outside the
    # vocabulary silently files the run against work it never did.
    try:
        declared = categories.validate(
            workflow_category=workflow_category,
            work_unit=work_unit,
            work_unit_count=work_unit_count,
        )
    except categories.CategoryError as exc:
        raise RunError(400, str(exc), allowed_categories=categories.category_ids()) from exc

    now = utcnow()
    basis = {
        "workflow_start_time": workflow_start_time_basis,
        "workflow_end_time": workflow_end_time_basis,
    }
    supplied = {
        "workflow_start_time": workflow_start_time,
        "workflow_end_time": workflow_end_time,
    }
    run = {
        "run_id": uuid.uuid4().hex[:12],
        "owner": owner,
        "status": "open",
        "created_at": now,
        "survey_started_at": now,
        "answers": {
            "workflow_name": name,
            "workflow_description": desc,
            "workflow_category": declared["workflow_category"],
            "work_unit": declared["work_unit"],
            "work_unit_count": declared["work_unit_count"],
        },
        # The derived half of the declaration, kept beside the run rather than
        # recomputed: whether this run can be normalised, and whether it may
        # enter a category cohort at all.
        "workflow_declaration": declared,
        "workflow_start_time": normalize(workflow_start_time),
        "workflow_end_time": normalize(workflow_end_time),
        "workflow_time_source": {
            key: (
                basis[key] or "supplied at run creation" if supplied[key] else "unset"
            )
            for key in ("workflow_start_time", "workflow_end_time")
        },
        "stage_markers": [],
        "calls": [],
        # Taken from the token, never from the request: an agent describes itself
        # once, at registration, and cannot relabel itself per submission.
        "agent_identity": agent_identity,
        # Whether anyone vetted the submitter, recorded when the run opens rather
        # than read back later: a token can be promoted or revoked afterwards, and
        # what matters is what was true when these numbers arrived.
        "token_corroborated": corroborated,
        "telemetry_adapter": telemetry_adapter,
        "runtime_metadata": _incoming_runtime_metadata(runtime_metadata),
        "grading": None,
        "source_inventory": (
            config.INVENTORY_CSV.name if config.PUBLIC_MODE else str(config.INVENTORY_CSV)
        ),
    }
    storage.save_run(run)
    return run, declared


def close(
    run: dict,
    *,
    grade=None,
    justification=None,
    evidence=None,
    grader: str = "self",
    workflow_name=None,
    workflow_description=None,
    workflow_end_time=None,
    allow_empty_telemetry: bool = False,
) -> tuple[dict, list[str]]:
    """Grade the run, build the submission, and say what is wrong with it.

    Returns the finished document and the warnings that belong with it. What
    happens to the document next - stored here, or sealed and posted from a
    collector - is the caller's business.
    """
    if run["status"] == "submitted":
        raise RunError(
            409,
            f"Run {run['run_id']} was already submitted.",
            response_path=run.get("response_path"),
        )

    if workflow_name:
        run["answers"]["workflow_name"] = clean_text(workflow_name, "workflow_name")
    if workflow_description:
        run["answers"]["workflow_description"] = clean_text(
            workflow_description, "workflow_description"
        )

    if not run["calls"] and not allow_empty_telemetry:
        raise RunError(
            422,
            "Refusing to submit a survey with no recorded telemetry: the measurement "
            "fields would be empty. Collect telemetry first, or pass "
            "allow_empty_telemetry=true to record a deliberately unmeasured run.",
        )

    try:
        grade_record = grading.validate(
            grade=grade,
            justification=justification,
            evidence=evidence,
            grader=grader,
        )
    except grading.GradingError as exc:
        raise RunError(400, str(exc), allowed_grades=grading.grade_codes()) from exc

    if config.PUBLIC_MODE:
        # The grade is the agent describing its own work, so it is the one place
        # left where the submitter's filesystem reaches the stored response.
        grade_record["evidence"] = [
            redact_evidence(e) for e in (grade_record.get("evidence") or [])
        ]
        if grade_record.get("justification"):
            grade_record["justification"] = scrub_local_paths(grade_record["justification"])

    run["grading"] = grade_record

    # A run with no call records still has real boundaries if its markers were
    # observed arriving during the work. That is the only timing a browser agent
    # can produce that nobody had to be believed for, so it is worth taking.
    observed = [
        m for m in run["stage_markers"]
        if m.get("marked_at_source") == "observed when the marker arrived"
    ]
    if observed and not run["calls"]:
        if not run.get("workflow_start_time"):
            run["workflow_start_time"] = observed[0]["marked_at"]
            run["workflow_time_source"]["workflow_start_time"] = (
                "observed when the first stage marker arrived, during the work"
            )
        closing = [m for m in observed if m.get("closes")]
        if closing and not run.get("workflow_end_time"):
            run["workflow_end_time"] = closing[-1]["marked_at"]
            run["workflow_time_source"]["workflow_end_time"] = (
                "observed when the closing stage marker arrived, during the work"
            )

    if workflow_end_time:
        run["workflow_end_time"] = normalize(workflow_end_time)
        run["workflow_time_source"]["workflow_end_time"] = "explicitly supplied by the agent"
    elif not run.get("workflow_end_time"):
        run["workflow_end_time"] = run.get("survey_started_at") or utcnow()
        run["workflow_time_source"]["workflow_end_time"] = (
            "observed when the survey was opened; the work finished before it"
        )

    response = compute.build_response(run)
    warnings = []
    if not response["pricing_resolution"].get("resolved"):
        warnings.append(
            f"Pricing could not be resolved for model "
            f"{response['pricing_resolution'].get('model_name')!r}; "
            "input_price_per_1m and output_price_per_1m are null and no cost was derived."
        )
    if not run["calls"]:
        warnings.append("Submitted with no telemetry: all measurement fields are null.")
    unmeasured = [c for c in run["calls"] if c.get("duration_basis") == "unmeasured"]
    if unmeasured:
        warnings.append(
            f"{len(unmeasured)} of {len(run['calls'])} calls had no observable trigger "
            "event; their elapsed time is recorded as 0 s and total_agent_runtime is "
            "therefore a lower bound."
        )
    if not run["stage_markers"]:
        warnings.append(
            "No workflow stages were declared, so the Agent Effort Profile is built "
            "from AMI-observed execution phases (lower provenance than declared stages)."
        )
    else:
        open_ended = compute.phases.open_ended_stage(run["stage_markers"])
        if open_ended:
            rows = response["agent_effort_profile"]
            tail = next(
                (r for r in rows if r.get("workflow_stage") == open_ended), None
            )
            total = sum(r["stage_agent_call_count"] for r in rows) or 1
            share = round(100 * (tail["stage_agent_call_count"] if tail else 0) / total)
            warnings.append(
                f"The final stage {open_ended!r} has no closing marker, so it runs to "
                f"the end of the measurement window and holds {share}% of the calls. "
                "Every other stage is bounded by the next marker; this one also "
                "contains whatever happened after the workflow finished - verifying "
                "output, the closing message to the human, retried survey calls. Read "
                "that row as 'this stage plus everything after it'. To bound it, call "
                "ami_mark_stage(closes=true) when the last stage is done."
            )

    if response["trust"]["tier"] == "reported":
        warnings.append(
            "Self-reported telemetry: these numbers were supplied by the submitting "
            "agent, not read from a runtime's own records. Comparable with other "
            "reported runs; not with measured ones."
        )
    for failed in response["plausibility"]["failed"]:
        detail = next(
            c["detail"] for c in response["plausibility"]["checks"]
            if c["check"] == failed
        )
        warnings.append(f"Plausibility check {failed!r} failed: {detail}")

    missing = [k for k, v in response["fields"].items() if v in (None, "")]
    if missing:
        warnings.append(f"Fields with no value: {', '.join(missing)}")

    response |= {
        "run_id": run["run_id"],
        "survey_id": "ami-collection-inventory",
        "submitted_at": utcnow(),
        "source_inventory": run.get("source_inventory", config.INVENTORY_CSV.name),
        "telemetry_adapter": run.get("telemetry_adapter"),
        "measurement_window": run.get(
            "measurement_window",
            {"start": run.get("workflow_start_time"), "end": run.get("survey_started_at")},
        ),
        "stage_markers": run["stage_markers"],
        "workflow_time_source": run["workflow_time_source"],
        "warnings": warnings,
    }
    return response, warnings


def describe_open_run(run: dict, declared: dict, requested_name: str | None = None) -> dict:
    """What a caller is told when a run opens.

    Shaping lives here rather than in whoever called, because two callers now
    report it - the hosted API and the collector on a tester's machine - and an
    agent should not be able to tell which one it is talking to.
    """
    name = run["answers"]["workflow_name"]
    result = {
        "run_id": run["run_id"],
        "status": run["status"],
        "survey_started_at": run["survey_started_at"],
        # Echoed so the caller can see exactly what was stored - this is the label
        # runs group by, and a transport-mangled name is otherwise invisible.
        "workflow_name": name,
        "workflow_description": run["answers"]["workflow_description"],
        "workflow_category": declared["workflow_category"],
        "next_steps": [
            "Collect telemetry for this run (ami_collect_telemetry, or POST "
            f"/runs/{run['run_id']}/calls for non-Claude-Code runtimes).",
            "Grade the workflow output against the AMI grading scale.",
            f"Submit: POST /runs/{run['run_id']}/submit",
        ],
        "fields_you_must_answer": [
            {"field": f.name, "question": f.question}
            for f in inventory.agent_answered_fields()
        ],
    }
    if name != (requested_name or "").strip():
        result["normalization_note"] = (
            "workflow_name arrived HTML-escaped and was unescaped before storing, so "
            f"this run groups with other runs named {name!r}."
        )
    return result


def describe_submission(run: dict, warnings: list[str]) -> dict:
    """A confirmation, and nothing a submitter could read their way back in with.

    The survey collects; what is made of the collection happens elsewhere, so
    there is no result to hand back and no link to follow. The warnings stay,
    because they are how an agent learns it collected badly - empty telemetry, an
    unbounded final stage, numbers recorded as a claim rather than a measurement.
    Silence there would make bad data likelier, not tidier.
    """
    return {
        "run_id": run["run_id"],
        "status": "submitted",
        "message": "Survey recorded.",
        "warnings": warnings,
    }
