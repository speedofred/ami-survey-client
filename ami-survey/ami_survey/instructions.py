"""The survey procedure, packaged for whatever runtime is asking.

A Claude Code "skill" is a markdown file with YAML frontmatter that its harness
loads automatically. Other runtimes have no such concept - but the procedure
itself is not Claude-specific, and neither are the tools. So there is one source
of truth (`skills/ami-survey/SKILL.md`) and this module re-packages it:

    instructions(runtime)   the procedure as plain markdown, with the telemetry
                            step adapted to what that runtime can actually measure
    tool_schemas(fmt)       the ami_* tools as OpenAI / Anthropic / Gemini function
                            definitions, for agents that are not MCP clients

The honest constraint, stated in the text itself: instructions travel anywhere,
but *telemetry needs an adapter per harness*. A runtime with one (see
`adapters/`) is measured from its own session log and the agent supplies nothing.
A runtime without one must report its own usage via `ami_record_calls`, because
the one thing the survey will not accept is a model's estimate of its own token
count.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

SKILL_FILE = config.PACKAGE_ROOT / "skills" / "ami-survey" / "SKILL.md"

RUNTIMES = {
    "claude-code": (
        "You are running in Claude Code, so `ami_collect_telemetry` can measure this "
        "run by reading the runtime's own transcript. Follow the procedure as written."
    ),
    "codex": (
        "You are running in Codex, so `ami_collect_telemetry` can measure this run by "
        "reading the session's own rollout. Follow the procedure as written - the "
        "survey works out which runtime you are in on its own."
    ),
    "mcp": (
        "You are an MCP client. `ami_collect_telemetry` detects which agent runtime "
        "you are in and reads that runtime's own records; adapters exist for "
        "{adapters}. If it reports that it cannot find a session log for your runtime, "
        "use `ami_record_calls` instead and pass the usage your own runtime reported "
        "for every API call the workflow made - `model`, `start_time`, `end_time`, "
        "`input_tokens`, `output_tokens`, read from real API responses. If your "
        "runtime does not expose usage, say so plainly rather than approximating: an "
        "approximated benchmark is worse than a missing one."
    ),
    "http": (
        "You are calling the survey over plain HTTP rather than MCP, so there is no "
        "runtime detection: each `ami_*` tool "
        "below maps to one endpoint on the survey API:\n\n"
        "    ami_survey_begin       POST /runs\n"
        "    ami_mark_stage         POST /runs/{run_id}/stages\n"
        "    ami_record_calls       POST /runs/{run_id}/calls\n"
        "    ami_get_grading_scale  GET  /survey/grading-scale\n"
        "    ami_survey_status      GET  /runs/{run_id}/preview\n"
        "    ami_submit_survey      POST /runs/{run_id}/submit\n\n"
        "There is no transcript to read, so telemetry comes from `POST /runs/{run_id}/"
        "calls` with the usage your own runtime reported for each API call - values read "
        "from real API responses, never estimated. `run_id` comes back from POST /runs; "
        "there is no ambient 'current run' outside the MCP server."
    ),
}

MEASUREMENT_HEADER = "# AMI workflow survey\n\n"


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL).lstrip()


def instructions(runtime: str = "mcp") -> str:
    """The procedure, with a preamble telling this runtime how it gets measured."""
    if runtime not in RUNTIMES:
        raise ValueError(
            f"unknown runtime {runtime!r}. Available: {', '.join(sorted(RUNTIMES))}"
        )
    from . import adapters

    body = _strip_frontmatter(SKILL_FILE.read_text())
    body = body.split("\n", 1)[1].lstrip() if body.startswith("# ") else body
    # A plain replace, not str.format: the HTTP text contains literal {run_id}.
    preamble = RUNTIMES[runtime].replace("{adapters}", ", ".join(adapters.available()))
    return f"{MEASUREMENT_HEADER}> **How this runtime is measured.** {preamble}\n\n{body}"


# --------------------------------------------------------------------------- #
# tool schemas for agents that are not MCP clients
# --------------------------------------------------------------------------- #

def _mcp_tools() -> list[dict]:
    # Imported lazily: the MCP server imports the API client at module load, and
    # the schemas are only needed when someone actually asks for them.
    from .mcp_server import TOOL_SPECS

    return TOOL_SPECS


FORMATS = ("mcp", "openai", "anthropic", "gemini")


def tool_schemas(fmt: str = "openai") -> list[dict]:
    """The ami_* tools in one provider's function-calling format."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}. Available: {', '.join(FORMATS)}")
    specs = _mcp_tools()
    if fmt == "mcp":
        return specs
    if fmt == "openai":
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                },
            }
            for t in specs
        ]
    if fmt == "anthropic":
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["inputSchema"],
            }
            for t in specs
        ]
    return [
        {
            "functionDeclarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                }
                for t in specs
            ]
        }
    ]


def bundle(runtime: str = "mcp", fmt: str = "openai") -> dict:
    """Everything a non-Claude agent needs, in one object."""
    return {
        "runtime": runtime,
        "instructions": instructions(runtime),
        "tool_format": fmt,
        "tools": tool_schemas(fmt),
        "api_url": config.API_URL,
        "mcp_command": "python3 -m ami_survey.mcp_server",
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

USAGE = """\
ami-skill - the AMI survey procedure and tools, packaged for any runtime.

    ami-skill                              the instructions (generic MCP client)
    ami-skill --runtime claude-code        the instructions as Claude Code gets them
    ami-skill --runtime http               the instructions for a plain-HTTP caller
    ami-skill --tools openai               tool schemas as OpenAI function defs
    ami-skill --tools anthropic|gemini|mcp tool schemas in another format
    ami-skill --bundle --tools gemini      instructions + tools as one JSON object
    ami-skill --out DIR                    write a Claude-style skill directory

Runtimes: {runtimes}
Formats:  {formats}
"""


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ami-skill", add_help=False,
        usage=USAGE.format(runtimes=", ".join(sorted(RUNTIMES)), formats=", ".join(FORMATS)),
    )
    parser.add_argument("--runtime", default="mcp", choices=sorted(RUNTIMES))
    parser.add_argument("--tools", default=None, choices=FORMATS)
    parser.add_argument("--bundle", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args(argv)

    if args.help:
        print(USAGE.format(
            runtimes=", ".join(sorted(RUNTIMES)), formats=", ".join(FORMATS)
        ))
        return 0

    if args.out:
        out = Path(args.out).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        (out / "SKILL.md").write_text(instructions(args.runtime))
        (out / "tools.json").write_text(
            json.dumps(tool_schemas(args.tools or "openai"), indent=2)
        )
        print(f"wrote {out / 'SKILL.md'}\nwrote {out / 'tools.json'}")
        return 0

    if args.bundle:
        print(json.dumps(bundle(args.runtime, args.tools or "openai"), indent=2))
    elif args.tools:
        print(json.dumps(tool_schemas(args.tools), indent=2))
    else:
        print(instructions(args.runtime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
