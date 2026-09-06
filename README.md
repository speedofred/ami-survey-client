# AMI survey — client

Measures what an agent workflow actually cost to run, and submits the result to
the AMI survey service.

**Start here: [GETTING-STARTED.md](GETTING-STARTED.md).** It assumes no prior
setup and covers macOS, Linux and Windows.

**Not on Claude Code or Codex?** You do not need this repo. In claude.ai, add
`https://submit.agentbenchmark.dev/mcp` as a custom connector — Settings →
Connectors → Add custom connector — and ask your agent to take the survey.
Nothing to install and no token to paste. Those runs are recorded as
`reported` rather than `measured`, since a remote server cannot read your
runtime's logs; install this client when you want the token counts and cost
measured rather than dictated.

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
| `ami_survey/submission.py` | seals the finished run and posts it |
| `amitransport/` | the sealing itself — the one part that needs a package |
| `skills/` | the procedure, in the skill format Claude Code and Codex both read |
| `scripts/install.py` | wires the above into your agent |
| `workflows/` | a sample workflow to practise on |
| `bin/` | the commands below |

The scoring itself — how a run becomes a scorecard, and what it is ranked
against — is not in this repository and does not run on your machine. This half
measures; that half scores.

Submissions go to **`submit.agentbenchmark.dev`** and nowhere else. That is a
constant in the source, not a setting: a result that never left your machine
would not be comparable with anyone else's, which is the entire point.

They go **sealed**. Your run is encrypted here, to a public key whose private
half exists only on the scoring server — so the server that receives and holds
your submission cannot read it. That is why sealing is the one thing in this
client that needs a third-party package.

A token is registered for you at install time; you do not need one in advance.

## Install

```bash
python3 ami-survey/scripts/install.py --user
```

Press Enter at the token prompt and it registers one for you. Then restart your
agent. `GETTING-STARTED.md` has the Windows form, the Codex form, and what to do
when it does not work.

Install PyNaCl first if you have not: `python3 -m pip install --user PyNaCl`.
Without it the tools appear in your agent and fail the moment one is called.

## Commands

In `ami-survey/bin/`. Each is a wrapper that sets `PYTHONPATH` and runs a module,
so they work from a clone with nothing installed. `--help` on any of them prints
its full flags.

### Managing workflows

**`ami-workflow`** lists, scaffolds and prepares them. It never runs anything —
your agent does that, under the AMI survey tools. They are separate commands so
that preparing a prompt and running it are not one word apart.

```bash
ami-survey/bin/ami-workflow list
ami-survey/bin/ami-workflow new my-workflow
ami-survey/bin/ami-workflow show support-ticket-triage
ami-survey/bin/ami-workflow show support-ticket-triage --prompt   # just the prompt
ami-survey/bin/ami-workflow list --dir ~/Downloads/handover
```

`show` clears the workflow's `output/` first, because a benchmark is only
comparable if every run starts from the same state, then prints the two messages
to send: the workflow prompt, and the separate request that closes the
measurement window. `--no-reset` prints without clearing.

### The rest

**`ami-session`** — which session the adapter would measure, and why. The first
thing to run when a survey measured the wrong thing.

```bash
ami-survey/bin/ami-session          # the session this survey would read
ami-survey/bin/ami-session --all    # every session it can see
```

**`ami-skill`** — the survey procedure and tool schemas, for a runtime that is
not an MCP client.

```bash
ami-survey/bin/ami-skill                          # the procedure
ami-survey/bin/ami-skill --runtime http           # for a plain-HTTP caller
ami-survey/bin/ami-skill --tools openai           # schemas as function defs
```

**`ami-mcp`** — the MCP server your agent launches. `scripts/install.py` writes
it into your agent's configuration; you rarely run it yourself.

Reading the collected results is not here, and there is no self-service page for
it yet: ask whoever issued your token for your scorecard.

## Removing it

```bash
python3 ami-survey/scripts/uninstall.py
```

## What leaves your computer

A survey response: token counts, timings, model names, the stage names your
workflow declared, and the grade. Not your files, not your prompts, not your
shell commands. `GETTING-STARTED.md` sets this out in full.

## Requirements

Python 3.9 or newer, and **PyNaCl**:

```bash
python3 -m pip install --user PyNaCl
```

That is the only dependency. Everything else is the standard library. On Debian
and Ubuntu, where `pip` refuses with "externally-managed-environment", use
`sudo apt install python3-nacl`.

## Licence

Evaluation only; see [LICENSE](LICENSE). This is not open source.
