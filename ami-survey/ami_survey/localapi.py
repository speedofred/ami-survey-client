"""The survey's own endpoints, answered in-process instead of over HTTP.

A collector on a tester's machine is the same survey as the hosted one; it just
has nobody to authenticate and nowhere to go. So the routes the MCP tools call
are answered here, from `runs` and the reference data, and the tools cannot tell
the difference - which is the point. An agent following the procedure gets the
same tool output whether the run is being assembled locally or by a server.

Nothing here authenticates anything, because there is nobody else on this
machine to keep out. What it does instead is keep the call records here: they
carry prompts and shell commands, and the whole reason for building the
submission locally is that they should be sealed before they travel.
"""

from __future__ import annotations

import re

from . import categories, config, grading, instructions, inventory, runs, storage, submission


class LocalCallFailed(Exception):
    """Raised with the status an HTTP caller would have seen.

    The MCP tools already branch on `status` - `mark_stage` buffers a marker on a
    404 or 409 rather than failing - so the local path has to speak the same
    language or that handling silently stops working.
    """

    def __init__(self, status: int, payload: dict):
        super().__init__(f"{status}: {payload.get('error')}")
        self.status = status
        self.payload = payload


def _run_or_404(run_id: str) -> dict:
    run = storage.load_run(run_id)
    if run is None:
        raise LocalCallFailed(404, {"error": f"No such run: {run_id}"})
    return run


def _open_run(body: dict) -> dict:
    run, declared = runs.new_run(
        workflow_name=body.get("workflow_name"),
        workflow_description=body.get("workflow_description"),
        workflow_category=body.get("workflow_category"),
        work_unit=body.get("work_unit"),
        work_unit_count=body.get("work_unit_count"),
        workflow_start_time=body.get("workflow_start_time"),
        workflow_end_time=body.get("workflow_end_time"),
        workflow_start_time_basis=body.get("workflow_start_time_basis"),
        workflow_end_time_basis=body.get("workflow_end_time_basis"),
        telemetry_adapter=body.get("telemetry_adapter"),
        runtime_metadata=body.get("runtime_metadata"),
        # No token, so nobody to be. Recorded as uncorroborated rather than
        # assumed trustworthy: a run built on the submitter's own machine is
        # exactly the case where nobody independent has checked anything.
        owner=None,
        agent_identity=None,
        corroborated=False,
    )
    return runs.describe_open_run(run, declared, body.get("workflow_name"))


def _record_calls(run_id: str, body: dict) -> dict:
    run = _run_or_404(run_id)
    totals = runs.record_calls(
        run,
        calls=body.get("calls"),
        adapter=body.get("adapter"),
        runtime_metadata=body.get("runtime_metadata"),
        replace=body.get("replace", True),
        default_workflow_start_time=body.get("default_workflow_start_time"),
        measurement_window=body.get("measurement_window"),
    )
    return {"run_id": run["run_id"], **totals, "completeness": runs.completeness(run)}


def _mark_stage(run_id: str, body: dict) -> dict:
    run = _run_or_404(run_id)
    markers = runs.mark_stage(
        run,
        stage=body.get("stage"),
        closes=body.get("closes"),
        marked_at=body.get("marked_at"),
        note=body.get("note"),
        recorded_before_run_opened=body.get("recorded_before_run_opened"),
        marked_at_claimed=body.get("marked_at_claimed"),
        marked_at_source=body.get("marked_at_source"),
    )
    return {"run_id": run["run_id"], "stage_markers": markers}


def _set_answers(run_id: str, body: dict) -> dict:
    run = _run_or_404(run_id)
    answers = runs.set_answers(
        run,
        workflow_name=body.get("workflow_name"),
        workflow_description=body.get("workflow_description"),
        workflow_start_time=body.get("workflow_start_time"),
        workflow_end_time=body.get("workflow_end_time"),
    )
    return {"run_id": run["run_id"], "answers": answers, "completeness": runs.completeness(run)}


def _submit(run_id: str, body: dict) -> dict:
    """Close the run, seal it, and hand it to the landing server.

    This is where the local path stops resembling the hosted one. Nothing is
    stored for anyone else to read: the finished document is sealed to a key only
    the AMI server holds, and what stays behind is the run this machine already
    had.
    """
    run = _run_or_404(run_id)
    response, warnings = runs.close(
        run,
        grade=body.get("agent_output_grade"),
        justification=body.get("grade_justification"),
        evidence=body.get("grade_evidence"),
        grader=body.get("grader", "self"),
        workflow_name=body.get("workflow_name"),
        workflow_description=body.get("workflow_description"),
        workflow_end_time=body.get("workflow_end_time"),
        allow_empty_telemetry=body.get("allow_empty_telemetry"),
    )

    try:
        receipt = submission.submit(response)
    except submission.SubmissionError as exc:
        # The run stays open and stays on disk. A submission that could not be
        # handed over is not a submission that should be forgotten - the work is
        # done and the document is built, so this is worth retrying rather than
        # re-running a workflow for.
        raise LocalCallFailed(502, {
            "error": f"The survey was built but could not be submitted: {exc}",
            "run_id": run["run_id"],
            "retry": "Fix the problem and call ami_submit_survey again; the run is still open.",
        }) from exc

    run["status"] = "submitted"
    run["submitted_at"] = response["submitted_at"]
    run["submission_id"] = receipt["submission_id"]
    storage.save_run(run)
    return {
        **runs.describe_submission(run, warnings),
        "submission_id": receipt["submission_id"],
        "transport": receipt.get("transport"),
    }


def _submitted() -> list[dict]:
    """What this machine has submitted, which is not what it holds.

    A collector keeps no responses: the document is sealed and handed over, and
    nothing readable stays behind. What can honestly be listed is which runs went
    and under what submission id - enough to ask about one later, and no copy of
    anything the landing server could not read either.
    """
    return [
        {
            "run_id": run.get("run_id"),
            "workflow_name": (run.get("answers") or {}).get("workflow_name"),
            "submitted_at": run.get("submitted_at"),
            "submission_id": run.get("submission_id"),
        }
        for run in storage.list_runs()
        if run.get("status") == "submitted"
    ]


#: (method, compiled path) -> handler. Deliberately only the routes a collector
#: needs: there is no /tokens here, and no /responses belonging to anyone else.
ROUTES = [
    ("GET", re.compile(r"^/health$"), lambda rid, body: {"status": "ok", "mode": "local collector"}),
    ("GET", re.compile(r"^/survey$"), lambda rid, body: inventory.survey_document()),
    ("GET", re.compile(r"^/survey/grading-scale$"), lambda rid, body: grading.scale()),
    ("GET", re.compile(r"^/survey/workflow-categories$"), lambda rid, body: categories.vocabulary()),
    ("GET", re.compile(r"^/instructions/(?P<runtime>[a-z0-9_-]+)$"),
     lambda rid, body: instructions.instructions(rid)),
    ("GET", re.compile(r"^/instructions$"), lambda rid, body: instructions.instructions("mcp")),
    ("GET", re.compile(r"^/responses$"), lambda rid, body: {"responses": _submitted()}),
    ("POST", re.compile(r"^/runs$"), lambda rid, body: _open_run(body)),
    ("GET", re.compile(r"^/runs$"), lambda rid, body: {"runs": storage.list_runs()}),
    ("POST", re.compile(r"^/runs/(?P<run_id>[A-Za-z0-9_-]+)/calls$"), _record_calls),
    ("POST", re.compile(r"^/runs/(?P<run_id>[A-Za-z0-9_-]+)/stages$"), _mark_stage),
    ("POST", re.compile(r"^/runs/(?P<run_id>[A-Za-z0-9_-]+)/answers$"), _set_answers),
    ("POST", re.compile(r"^/runs/(?P<run_id>[A-Za-z0-9_-]+)/submit$"), _submit),
    ("GET", re.compile(r"^/runs/(?P<run_id>[A-Za-z0-9_-]+)/preview$"),
     lambda rid, body: runs.preview(_run_or_404(rid))),
    ("GET", re.compile(r"^/runs/(?P<run_id>[A-Za-z0-9_-]+)$"),
     lambda rid, body: {**_run_or_404(rid), "completeness": runs.completeness(_run_or_404(rid))}),
]


def handle(method: str, path: str, body: dict | None = None):
    """Answer one call, or raise what an HTTP caller would have been given."""
    for route_method, pattern, handler in ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        captured = match.groupdict()
        argument = captured.get("run_id") or captured.get("runtime")
        try:
            return handler(argument, body or {})
        except runs.RunError as exc:
            raise LocalCallFailed(exc.status, {"error": exc.message, **exc.extra}) from exc
        except ValueError as exc:
            raise LocalCallFailed(404, {"error": str(exc)}) from exc
    raise LocalCallFailed(404, {"error": f"No local route for {method} {path}"})
