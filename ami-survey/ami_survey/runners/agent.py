"""Run a workflow on any supported provider, and survey it with the same
instrumentation a Claude Code run gets.

    ami-run support-ticket-triage --provider openai    --model gpt-4.1
    ami-run support-ticket-triage --provider gemini    --model gemini-2.5-pro
    ami-run support-ticket-triage --provider anthropic --model claude-sonnet-4-5
    ami-run support-ticket-triage --provider openai    --model llama3.3 \
        --base-url http://localhost:11434/v1 --api-key-env OLLAMA_API_KEY

Everything provider-specific lives in `dialects.py`. This module is the agent
loop, the sandboxed workspace, and the survey submission - identical for every
model, which is what makes the resulting numbers comparable.

Every measurement comes from the provider's own response: `usage` for tokens,
the response's model id for what actually served the request, and the client's
clock for the request/response boundary. Nothing is estimated. The self-grading
call at the end is deliberately excluded from the telemetry, exactly as the
survey's own token spend is excluded from a Claude Code run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .. import client, workflow as workflow_mod
from ..timeutil import iso
from . import dialects

MAX_TOOL_RESULT_CHARS = 100_000

RunnerError = dialects.DialectError


def _now() -> str:
    return iso(datetime.now(timezone.utc))


class Workspace:
    """The file tools, confined to the workflow directory.

    A model is writing files on a real machine, so every path is resolved and
    checked against the root before anything is opened.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.stage_markers: list[dict] = []
        # The prompt is written for an agent sitting in the project root, so it
        # says "workflows/<name>/tickets". The workspace root IS that
        # directory, so that prefix is stripped rather than 404ing the model.
        self.rel_root = f"{self.root.parent.name}/{self.root.name}"

    def _strip_root_prefix(self, rel: str) -> str:
        parts = PurePosixPath(rel).parts
        if self.root.name in parts:
            return "/".join(parts[parts.index(self.root.name) + 1:])
        return rel

    def _resolve(self, raw: str) -> Path:
        rel = (raw or "").strip()
        if rel.startswith("/") or rel.startswith("~"):
            # Reinterpreting an absolute path as workspace-relative would quietly
            # write somewhere the model did not ask for; say so instead.
            raise RunnerError(f"paths are workspace-relative, not absolute: {raw!r}")
        candidate = (self.root / self._strip_root_prefix(rel)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise RunnerError(f"path escapes the workspace: {raw!r}")
        return candidate

    def list_files(self, path: str = "") -> str:
        target = self._resolve(path or ".")
        if not target.is_dir():
            return f"not a directory: {path}"
        lines = []
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue
            rel = child.relative_to(self.root)
            lines.append(f"{rel}/" if child.is_dir() else f"{rel} ({child.stat().st_size} bytes)")
        return "\n".join(lines) or "(empty)"

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"no such file: {path}"
        return target.read_text(encoding="utf-8")[:MAX_TOOL_RESULT_CHARS]

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {target.relative_to(self.root)}"

    def mark_stage(self, stage: str, note: str | None = None) -> str:
        """Recorded with the time the model actually emitted it, then posted to the
        survey once the run exists - the same contract as ami_mark_stage."""
        self.stage_markers.append({"stage": stage, "marked_at": _now(), "note": note})
        return f"stage marked: {stage}"

    def dispatch(self, name: str, args: dict) -> str:
        fn = {
            "list_files": lambda: self.list_files(args.get("path", "")),
            "read_file": lambda: self.read_file(args.get("path", "")),
            "write_file": lambda: self.write_file(args.get("path", ""), args.get("content", "")),
            "mark_stage": lambda: self.mark_stage(args.get("stage", ""), args.get("note")),
        }.get(name)
        if fn is None:
            return f"unknown tool: {name}"
        if "__invalid_arguments__" in args:
            return "error: the tool arguments were not valid JSON; send them again"
        try:
            return str(fn())[:MAX_TOOL_RESULT_CHARS]
        except (RunnerError, OSError, UnicodeDecodeError) as exc:
            return f"error: {exc}"


SYSTEM_PROMPT = (
    "You are an agent working in a file workspace. Use the tools to read the "
    "inputs and write the outputs the task asks for; do not print file contents "
    "as your answer instead of writing them. All paths are relative to the "
    "workspace root, which is the {root} directory itself - so read 'tickets/x.md', "
    "not '{root}/tickets/x.md'. Call mark_stage as you enter each phase of the "
    "task, at the moment you enter it. When the task is complete, reply with a "
    "short summary and no further tool calls."
)


def run_workflow(
    workflow: workflow_mod.Workflow,
    dialect: dialects.Dialect,
    model: str,
    max_steps: int,
    verbose: bool = True,
) -> dict:
    workspace = Workspace(workflow.path)
    dialect.begin(SYSTEM_PROMPT.format(root=workspace.rel_root), workflow.prompt())
    calls: list[dict] = []
    started = _now()

    for step in range(max_steps):
        body, start, end = dialect.send(model)
        turn = dialect.parse(body)
        calls.append(turn.call_record(start, end, step))
        dialect.record_assistant(body)

        if verbose:
            names = ", ".join(tc.name for tc in turn.tool_calls) or "(final answer)"
            print(
                f"  step {step + 1:>2}: {turn.input_tokens:>8,} in / "
                f"{turn.output_tokens:>6,} out  {names}",
                flush=True,
            )

        if not turn.tool_calls:
            return {
                "calls": calls,
                "stage_markers": workspace.stage_markers,
                "started": started,
                "ended": _now(),
                "final_message": turn.text,
                "stopped_because": "the model finished without further tool calls",
            }

        results = [workspace.dispatch(tc.name, tc.args) for tc in turn.tool_calls]
        dialect.record_tool_results(turn.tool_calls, results)

    return {
        "calls": calls,
        "stage_markers": workspace.stage_markers,
        "started": started,
        "ended": _now(),
        "final_message": "",
        "stopped_because": f"hit the {max_steps}-step limit before the model stopped",
    }


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #

GRADE_PROMPT = (
    "You have just finished this task:\n\n{task}\n\nYour summary of what you "
    "produced:\n\n{summary}\n\nGrade the OUTPUT you produced - the files, not your "
    "effort and not whether the run felt smooth. Be willing to grade your own work "
    "down.\n\nGrading scale:\n{scale}\n\nReply with JSON only, no prose and no code "
    'fence: {{"grade": "<code from the scale>", "justification": "<at least 40 '
    'characters, measured against the task\'s stated requirements>", "evidence": '
    '["<file path>", ...]}}'
)


def self_grade(dialect: dialects.Dialect, model: str, workflow: workflow_mod.Workflow, summary: str) -> dict:
    """One extra call, outside the measured window, mirroring the self-grade a
    Claude Code agent gives when it takes the survey."""
    scale = client.get("/survey/grading-scale")
    rendered = "\n".join(
        f"  {g['code']}: {g.get('label')} - {g.get('definition', '')}"
        for g in scale.get("grades", [])
    )
    reply = dialect.ask_once(
        model,
        "You grade your own work honestly and reply with JSON only.",
        GRADE_PROMPT.format(
            task=workflow.prompt(), summary=summary or "(work complete)", scale=rendered
        ),
    )
    text = reply.strip()
    if text.startswith("```"):  # some models fence JSON however firmly you ask
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        graded = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        raise RunnerError(
            f"the grading call did not return JSON: {reply[:200]!r}. Re-run with "
            "--grade skip and grade the output yourself."
        ) from None
    return {
        "grade": graded.get("grade"),
        "justification": graded.get("justification") or "",
        "evidence": graded.get("evidence") or workflow.meta.get("outputs") or [],
    }


# --------------------------------------------------------------------------- #
# survey submission
# --------------------------------------------------------------------------- #

def survey_run(workflow: workflow_mod.Workflow, result: dict, dialect: dialects.Dialect, grade: dict) -> dict:
    platform = f"ami-runner/{dialect.name}@{dialect.base_url}"
    created = client.post(
        "/runs",
        {
            "workflow_name": workflow.workflow_name,
            "workflow_description": workflow.workflow_description,
            "workflow_start_time": result["started"],
            "workflow_end_time": result["ended"],
            "workflow_start_time_basis": "first request the runner sent to the model",
            "workflow_end_time_basis": "last response the runner received",
            "telemetry_adapter": f"ami-runner/{dialect.name}",
            "runtime_metadata": {
                "runtime": "ami-runner",
                "entrypoint": dialect.name,
                "platform": platform,
                "usage_mapping": dialect.usage_note,
                "pricing_provider_hint": dialect.pricing_prefix,
            },
        },
    )
    run_id = created["run_id"]

    for marker in result["stage_markers"]:
        client.post(
            f"/runs/{run_id}/stages",
            {"stage": marker["stage"], "marked_at": marker["marked_at"],
             "note": marker.get("note")},
        )

    client.post(
        f"/runs/{run_id}/calls",
        {
            "calls": result["calls"],
            "adapter": f"ami-runner/{dialect.name}",
            "runtime_metadata": {"platform": platform},
            "measurement_window": {"start": result["started"], "end": result["ended"]},
            "replace": True,
        },
    )

    return client.post(
        f"/runs/{run_id}/submit",
        {
            "agent_output_grade": grade["grade"],
            "grade_justification": grade["justification"],
            "grade_evidence": grade["evidence"],
            "grader": grade.get("grader", "self"),
        },
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ami-run",
        description="Run a workflow on any provider's API and survey the result.",
        epilog=(
            "examples:\n"
            "  ami-run support-ticket-triage --provider openai --model gpt-4.1 --dry-run\n"
            "  ami-run support-ticket-triage --provider anthropic --model claude-sonnet-4-5\n"
            "  ami-run their-workflow --dir ~/Downloads/handover \\\n"
            "      --provider gemini --model gemini-2.5-pro\n"
            "  ami-run support-ticket-triage --provider openai --model llama3.3 \\\n"
            "      --base-url http://localhost:11434/v1 --api-key-env OLLAMA_API_KEY\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("workflow", help="workflow name, e.g. support-ticket-triage")
    workflow_mod.add_dir_argument(parser)
    parser.add_argument("--provider", default="openai",
                        choices=sorted(dialects.DIALECTS),
                        help="API dialect to speak (default: openai)")
    parser.add_argument("--model", help="model id (required unless --dry-run)")
    parser.add_argument("--base-url", default=None,
                        help="override the provider's base URL, e.g. a local server")
    parser.add_argument("--api-key-env", default=None,
                        help="environment variable holding the API key "
                             "(default: the provider's own)")
    parser.add_argument("--max-steps", type=int, default=40,
                        help="stop the agent loop after this many API calls (default: 40)")
    parser.add_argument("--max-tokens", type=int, default=8192,
                        help="per-response output cap where the provider requires one")
    parser.add_argument("--grade", default="auto",
                        help="'auto' to have the model grade itself, 'skip' to leave the "
                             "run unsubmitted, or a grade code to supply your own")
    parser.add_argument("--grade-justification", default="",
                        help="required when --grade is a grade code")
    parser.add_argument("--no-reset", action="store_true",
                        help="do not clear the workflow's output before running")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would run, without calling the model")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workflow_mod.use_dir(args.dir)

    try:
        workflow = workflow_mod.find(args.workflow)
        prompt = workflow.prompt()
        dialect_cls = dialects.get(args.provider)
    except (workflow_mod.WorkflowNotFound, dialects.DialectError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    key_env = args.api_key_env or dialect_cls.key_env
    base_url = args.base_url or dialect_cls.default_base_url
    api_key = os.environ.get(key_env, "")

    if args.dry_run:
        print(f"workflow:       {workflow.name} ({workflow.path})")
        print(f"groups as:  {workflow.workflow_name}")
        print(f"provider:   {dialect_cls.name}")
        print(f"model:      {args.model or '(none given)'}")
        print(f"endpoint:   {base_url}")
        print(f"api key:    {key_env} is "
              f"{'set' if api_key else 'NOT SET - the run would fail'}")
        print(f"usage:      {dialect_cls.usage_note}")
        print(f"tools:      {', '.join(t['name'] for t in dialects.TOOLS)}")
        print(f"max steps:  {args.max_steps}")
        print(f"grading:    {args.grade}")
        # Say what is *not* being sent, too. PROMPT.md legitimately holds more
        # than one `---` block - the shipped workflow keeps its survey request in a
        # later one - but only the first is ever run, and a workflow handed over
        # by someone else is exactly where that goes unnoticed.
        blocks = workflow.prompt_blocks()
        if len(blocks) > 1:
            print(f"\nPROMPT.md holds {len(blocks)} `---` blocks; only the first "
                  f"is sent. Ignored:")
            for i, other in enumerate(blocks[1:], start=2):
                first_line = other.splitlines()[0] if other.splitlines() else ""
                print(f"  block {i} ({len(other.split())} words): {first_line[:60]}")
        print(f"\nprompt ({len(prompt.split())} words):\n{'-' * 76}\n{prompt}\n{'-' * 76}")
        return 0

    if not args.model:
        print("--model is required (or use --dry-run).", file=sys.stderr)
        return 1
    if not api_key:
        print(
            f"{key_env} is not set. Export your key first:\n    export {key_env}=...",
            file=sys.stderr,
        )
        return 1

    if not args.no_reset:
        for action in workflow_mod.reset(workflow)["actions"]:
            print(f"reset: {action}")

    dialect = dialect_cls(base_url, api_key, max_tokens=args.max_tokens)
    print(f"\nrunning {workflow.name} on {args.model} via {dialect.name} at {base_url}\n")
    wall_start = time.time()
    try:
        result = run_workflow(workflow, dialect, args.model, args.max_steps)
    except RunnerError as exc:
        print(f"\nrun failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"\n{len(result['calls'])} API call(s), "
        f"{len(result['stage_markers'])} stage marker(s), "
        f"{time.time() - wall_start:.1f}s wall clock"
    )
    print(f"stopped: {result['stopped_because']}")

    if args.grade.lower() == "skip":
        print("\n--grade skip: nothing submitted. Grade the output, then re-run with "
              "--grade <code> --grade-justification '...' --no-reset.")
        return 0

    try:
        if args.grade.lower() == "auto":
            grade = self_grade(dialect, args.model, workflow, result["final_message"])
            grade["grader"] = "self"
        else:
            if len(args.grade_justification) < 40:
                print("--grade <code> needs --grade-justification of at least 40 characters.",
                      file=sys.stderr)
                return 1
            grade = {
                "grade": args.grade,
                "justification": args.grade_justification,
                "evidence": workflow.meta.get("outputs") or [workflow.name],
                "grader": "human",
            }
        submitted = survey_run(workflow, result, dialect, grade)
    except (RunnerError, client.ApiCallFailed, client.ApiUnavailable) as exc:
        print(f"\nsurvey failed: {exc}", file=sys.stderr)
        return 1

    f = submitted["fields"]
    d = submitted["derived_analysis"]
    print(f"\nsurvey submitted: run {submitted['run_id']}")
    print(f"  model            {f['model_name']} ({f['model_provider']})")
    print(f"  API calls        {f['total_api_request_count']}")
    print(f"  tokens           {f['input_tokens']:,} in / {f['output_tokens']:,} out")
    print(f"  agent runtime    {f['total_agent_runtime']} s")
    cost = d.get("estimated_cost_usd_cache_aware")
    print(f"  est. cost        {'$%.4f' % cost if cost is not None else 'unavailable'}")
    print(f"  grade            {f['agent_output_grade']} ({grade.get('grader')})")
    for w in submitted.get("warnings") or []:
        print(f"  warning: {w}")
    print(f"\n  ami-survey/bin/ami-report {submitted['run_id']}")
    print(f"  ami-survey/bin/ami-compare \"{workflow.workflow_name}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
