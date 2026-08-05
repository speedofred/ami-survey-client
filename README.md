# AMI survey — client

Measures what an agent workflow actually cost to run, and submits the result to
the AMI survey service.

**Start here: [GETTING-STARTED.md](GETTING-STARTED.md).** It assumes no prior
setup and covers macOS, Linux and Windows.

## What this is

Ask any agent *"Take the AMI survey regarding [your workflow]"* after it
finishes a piece of work, and it will report what that work cost — tokens,
calls, wall-clock time, model, price, and a graded assessment of the output —
without you filling anything in.

Every number comes from the runtime's own session records, not from the agent's
recollection. An agent asked how many tokens it just used will guess, and guess
confidently. This reads the log instead.

## What is in here

| | |
|---|---|
| `ami_survey/adapters/` | reads a harness's own session log — one adapter per harness, currently Claude Code and Codex |
| `ami_survey/mcp_server.py` | the `ami_*` tools your agent calls |
| `ami_survey/client.py` | talks to the survey service |
| `skills/` | the procedure, in the skill format Claude Code and Codex both read |
| `scripts/install.py` | wires the above into your agent |
| `demo-workflows/` | a sample workflow to practise on |

The survey service itself — the field definitions, scoring, pricing and storage —
is not in this repository. This half runs on your machine; that half runs on the
server, and the two speak over HTTP.

## Install

```bash
python3 ami-survey/scripts/install.py --user --api-url <survey-url>
```

Then restart your agent. `GETTING-STARTED.md` has the Windows form, the Codex
form, and what to do when it does not work.

## Removing it

```bash
python3 ami-survey/scripts/uninstall.py
```

## What leaves your computer

A survey response: token counts, timings, model names, the stage names your
workflow declared, and the grade. Not your files, not your prompts, not your
shell commands. `GETTING-STARTED.md` sets this out in full.

## Requirements

Python 3.9 or newer. Nothing to `pip install` — the standard library only.

## Licence

Evaluation only; see [LICENSE](LICENSE). This is not open source.
