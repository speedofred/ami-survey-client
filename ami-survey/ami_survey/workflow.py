"""Workflow discovery, scaffolding and reset.

A benchmark is only comparable if every run starts from the same state, so this
module knows two things: where the workflows live, and what has to be true before
a run begins.

`ami-workflow` manages workflows; `ami-run` is the only thing that runs one. The
split is deliberate - preparing a workflow to paste into Claude Code and spending
money on a provider's API should not be one word apart.

    ami-workflow list                  every workflow found
    ami-workflow new <name>            scaffold a directory
    ami-workflow show <name>           reset it and print the prompts to paste
    ami-workflow show <name> --prompt  just the prompt, for piping
    ami-workflow list --dir PATH       workflows kept somewhere else
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import config

# workflows/ sits beside ami-survey/ in the project root. `--dir` overrides it
# per invocation, which is what a workflow handed to you needs: an environment
# variable is invisible, persists into the next command, and silently points at
# the wrong directory when you forget you set it.
WORKFLOWS_DIR = Path(os.environ.get("AMI_WORKFLOWS_DIR") or config.PROJECT_ROOT / "workflows")


def use_dir(path: str | None) -> None:
    """Point this process at a workflow directory other than the default."""
    global WORKFLOWS_DIR
    if path:
        WORKFLOWS_DIR = Path(path).expanduser().resolve()


def add_dir_argument(parser) -> None:
    """The one flag both commands share, worded once."""
    parser.add_argument(
        "--dir", default=None, metavar="PATH",
        help="directory holding the workflows (default: workflows/ in the project root)")


class WorkflowNotFound(RuntimeError):
    pass


class Workflow:
    """One benchmarkable workflow: a prompt, its inputs, and where output goes."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.meta = self._read_meta()

    def _read_meta(self) -> dict:
        meta_file = self.path / "workflow.json"
        if not meta_file.exists():
            return {}
        try:
            return json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkflowNotFound(f"{meta_file} is unreadable: {exc}") from exc

    @property
    def workflow_name(self) -> str:
        """The label runs group by - identical across models and runtimes."""
        return self.meta.get("workflow_name") or self.name

    @property
    def workflow_description(self) -> str:
        return self.meta.get("workflow_description") or ""

    @property
    def output_dir(self) -> Path:
        return self.path / "output"

    def prompt_blocks(self) -> list[str]:
        """Every `---`-delimited block in PROMPT.md, in order.

        The first is the workflow prompt. The rest are legitimate - the shipped
        workflow uses a later block for the survey request a human sends as their
        second message - but only the first is ever run, and that is the part
        worth being able to see.
        """
        prompt_file = self.path / "PROMPT.md"
        if not prompt_file.exists():
            raise WorkflowNotFound(f"{self.name} has no PROMPT.md")
        others = sorted(
            q.name for q in self.path.glob("PROMPT*.md") if q.name != "PROMPT.md"
        )
        if others:
            # Unambiguous: somebody meant to supply more than one prompt. One
            # directory runs one prompt, so picking PROMPT.md silently would run
            # part of a workflow and measure it as though it were the whole.
            raise WorkflowNotFound(
                f"{self.name} has more than one prompt file: PROMPT.md and "
                f"{', '.join(others)}. A workflow directory runs exactly one "
                "prompt. Split them into a directory each, with their own "
                "workflow.json, or fold them into one prompt with stage markers."
            )
        blocks = [b.strip() for b in prompt_file.read_text().split("\n---\n")]
        if len(blocks) < 2 or not blocks[1]:
            raise WorkflowNotFound(
                f"{prompt_file} has no prompt block delimited by '---' rules."
            )
        return blocks[1:]

    def prompt(self) -> str:
        """The workflow prompt: the first `---`-delimited block in PROMPT.md."""
        return self.prompt_blocks()[0]

    def survey_request(self) -> str:
        """The second human turn: the one that closes the measurement window."""
        return (
            f"Take the AMI survey regarding the {self.name.replace('-', ' ')} "
            f"workflow. Use the workflow_name and workflow_description from "
            f"workflow.json so this run groups with the others."
        )


def find(name: str) -> Workflow:
    path = WORKFLOWS_DIR / name
    if not path.is_dir():
        available = ", ".join(d.name for d in discover()) or "none found"
        raise WorkflowNotFound(f"No workflow named {name!r} in {WORKFLOWS_DIR}. Have: {available}")
    return Workflow(path)


def discover() -> list[Workflow]:
    if not WORKFLOWS_DIR.is_dir():
        return []
    return [
        Workflow(p)
        for p in sorted(WORKFLOWS_DIR.iterdir())
        if p.is_dir() and not p.name.startswith("_") and (p / "PROMPT.md").exists()
    ]


# --------------------------------------------------------------------------- #
# reset
# --------------------------------------------------------------------------- #

def reset(workflow: Workflow) -> dict:
    """Return the workflow to its pre-run state.

    Only what lives beside the workflow, and the transient state this machine
    holds: the output directory, and the pointers an interrupted run leaves in
    the local run directory.

    Abandoned *runs* on a survey server are a different thing and are cleaned up
    there, with `ami-report --prune-abandoned`. They are not on a submitter's
    disk at all - a client posts to the hosted survey and keeps nothing - so
    reaching for them here would have meant this module knowing about a store
    that is not always present.
    """
    actions: list[str] = []

    removed = 0
    if workflow.output_dir.exists():
        for child in sorted(workflow.output_dir.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                removed += sum(1 for p in child.rglob("*") if p.is_file())
                shutil.rmtree(child)
            else:
                removed += 1
                child.unlink()
    workflow.output_dir.mkdir(parents=True, exist_ok=True)
    actions.append(f"cleared {removed} file(s) from {workflow.output_dir.relative_to(WORKFLOWS_DIR)}")

    # Transient MCP state. A pointer at a submitted run, or markers buffered by an
    # abandoned run, would otherwise bleed into the next benchmark.
    for pointer in (config.RUNS_DIR / ".current_run", config.RUNS_DIR / ".pending_stages.json"):
        if pointer.exists():
            pointer.unlink()
            actions.append(f"cleared {pointer.name}")

    return {"actions": actions}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_listing() -> int:
    workflows = discover()
    if not workflows:
        print(f"No workflows found in {WORKFLOWS_DIR}", file=sys.stderr)
        return 1
    print(f"Workflows in {WORKFLOWS_DIR}:\n")
    for d in workflows:
        print(f"  {d.name}")
        print(f"      groups as: {d.workflow_name}")
    print("\nReset one and get its prompts:  ami-workflow show <name>")
    return 0


SAFE_NAME = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")

#: The `---` rules are load-bearing: `Workflow.prompt()` takes the text between the
#: first pair as the prompt, so everything outside them is notes for whoever
#: opens the file. A scaffold without them produces a workflow `ami-run` refuses.
PROMPT_TEMPLATE = """\
# The workflow prompt

Notes to yourself go here, outside the rules. Everything between the two `---`
lines below is the prompt, verbatim - that and nothing else is what the model
receives.

---

You are working in `workflows/{name}/`.

<What the work is. Name the inputs the agent should read.>

Follow the standard in `standard.md`.

Write your result to `output/RESULT.md`.

---

Two rules worth keeping:

- **Name the output path.** The agent has four file tools and no way to guess
  where results should go.
- **Give it real stages, or none.** An invented boundary produces an invented
  effort breakdown.
"""

STANDARD_TEMPLATE = """\
# What a good output looks like

<!-- Delete this file if the agent should not see the standard, and put an
     answer key in workflows/_answer_keys/{name}.md instead. -->

Concrete enough that two people grading the same output would agree. For
example: required sections, a format, a tone, a length, a set of correct
answers.

A grade against no standard is the model's opinion of its own work, and it is
always a B.
"""


def scaffold(name: str) -> Path:
    """Create the directory `ami-run` needs, and refuse to touch an existing one.

    The layout is fixed, so this is deterministic rather than a judgement call -
    which is why it is a command and not a skill. Deciding the stages, the
    description and the standard is the judgement, and `ami-make-measurable`
    already does that; this just lays out the files it fills in.
    """
    if not SAFE_NAME.match(name):
        raise WorkflowNotFound(
            f"{name!r} is not a usable workflow name. Use lower-case words joined by "
            "hyphens, e.g. applicant-rejection-email - it becomes a directory."
        )
    path = WORKFLOWS_DIR / name
    if path.exists():
        # Never write over someone's workflow: the inputs and the prompt are the
        # benchmark, and a clobbered one cannot be recovered from the responses.
        raise WorkflowNotFound(f"{path} already exists. Pick another name, or edit it in place.")

    (path / "output").mkdir(parents=True)
    (path / "PROMPT.md").write_text(
        PROMPT_TEMPLATE.replace("{name}", name), encoding="utf-8")
    (path / "standard.md").write_text(
        STANDARD_TEMPLATE.replace("{name}", name), encoding="utf-8")
    # Keeps the directory in git, and ami-run clears the contents rather than the
    # directory itself.
    (path / "output" / ".gitkeep").write_text("", encoding="utf-8")
    json_path = path / "workflow.json"
    json_path.write_text(json.dumps({
        "workflow_name": name.replace("-", " ").title(),
        "workflow_description": (
            "<What comes in and what goes out, in one or two sentences. At least "
            "20 characters - it is stored with every run.>"
        ),
        "stages": [],
        "outputs": ["output/RESULT.md"],
    }, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ami-workflow",
        description=(
            "Manage benchmark workflows. Running one against a provider is "
            "`ami-run`; this lists, scaffolds and prepares them."
        ),
        epilog=(
            "examples:\n"
            "  ami-workflow list\n"
            "  ami-workflow new applicant-rejection-email\n"
            "  ami-workflow show support-ticket-triage\n"
            "  ami-workflow show their-workflow --dir ~/Downloads/handover\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="every workflow found")
    add_dir_argument(p_list)

    p_new = sub.add_parser("new", help="scaffold a workflow directory")
    p_new.add_argument("workflow", help="name, lower-case words joined by hyphens")
    add_dir_argument(p_new)

    p_show = sub.add_parser(
        "show",
        help="reset a workflow and print the prompts to paste into an agent")
    p_show.add_argument("workflow")
    p_show.add_argument("--prompt", action="store_true",
                        help="print only the workflow prompt, for piping")
    p_show.add_argument("--no-reset", action="store_true",
                        help="print the prompt without clearing anything")
    p_show.add_argument("--force-prompts", action="store_true",
                        help="print the prompts even when output is being captured")
    add_dir_argument(p_show)

    args = parser.parse_args(argv)
    use_dir(getattr(args, "dir", None))

    if args.command in (None, "list"):
        return _print_listing()

    if args.command == "new":
        try:
            path = scaffold(args.workflow)
        except (WorkflowNotFound, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Created {path}\n")
        for line in (
            "  workflow.json   name every run groups by - keep it stable forever",
            "  PROMPT.md       what the model receives; name the output path in it",
            "  standard.md     what a good output looks like; delete if the agent",
            "                  should not see it, and add an answer key instead",
            "  output/         cleared before each run",
        ):
            print(line)
        print(
            f"\nFill those in, then:\n"
            f"  ami-run {args.workflow} --dry-run\n\n"
            "BENCHMARKING.md has the walkthrough, including why the standard comes "
            "before the prompt."
        )
        return 0

    try:
        workflow = find(args.workflow)
        prompt = workflow.prompt()
    except WorkflowNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.prompt:
        print(prompt)
        return 0

    print(f"# {workflow.workflow_name}\n")
    if not args.no_reset:
        result = reset(workflow)
        for a in result["actions"]:
            print(f"  reset: {a}")
        print()

    # These prompts are written for a human to paste into a fresh agent session.
    # If an agent runs this command itself, the output lands in its context as
    # apparent instructions - including the survey request - and it will do the
    # whole benchmark in one turn, which destroys the measurement: the survey
    # then measures a run that is still in progress. So the prompts are withheld
    # unless a human is on the other end of stdout.
    if not (sys.stdout.isatty() or args.force_prompts):
        print(
            "Prompts withheld: stdout is not a terminal, so this output is being "
            "captured by another program.\n\n"
            "If you are an agent that ran this command: STOP HERE and hand back to "
            "the human. Do not start the workflow and do not take the AMI survey. "
            "The benchmark needs the human to paste the workflow prompt as one "
            "message and request the survey as a separate later message; doing both "
            "yourself makes the survey measure a run that has not finished.\n\n"
            f"Human: run `ami-workflow {workflow.name}` in your own terminal to see the two "
            "messages, or pass --force-prompts."
        )
        return 0

    print("Before you start, confirm the session is fresh:\n")
    print("    ami-survey/bin/ami-session\n")
    print("A fresh session shows 1 human turn. Then paste this as the first message:\n")
    print("-" * 76)
    print(prompt)
    print("-" * 76)
    print("\nWhen it stops, send this as a SEPARATE second message:\n")
    print("-" * 76)
    print(workflow.survey_request())
    print("-" * 76)
    print(
        f"\nOr run it against another model without Claude Code:\n\n"
        f"    ami-survey/bin/ami-run {workflow.name} --provider openai --model gpt-4.1\n"
        f"    ami-survey/bin/ami-run {workflow.name} --provider gemini --model gemini-2.5-pro\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
