# AMI survey — client

Measures what an agent workflow actually cost to run, and submits the result to
the AMI survey service.

**Start here: [GETTING-STARTED.md](GETTING-STARTED.md).** It assumes no prior
setup and covers macOS, Linux and Windows.

**Not on Claude Code or Codex?** You do not need this repo. In claude.ai, add
`https://survey.agentbenchmark.dev/mcp` as a custom connector — Settings →
Connectors → Add custom connector — and ask your agent to take the survey.
Nothing to install and no token to paste. Those runs are recorded as
`unmeasured`, since a remote server cannot read your runtime's logs; install
this client when you want the token counts and cost as well.

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
| `ami_survey/runners/` | drives a workflow against a provider's API, with your own key |
| `scripts/install.py` | wires the above into your agent |
| `workflows/` | a sample workflow to practise on |
| `bin/` | the commands below |

The survey service itself — the field definitions, scoring, pricing and storage —
is not in this repository. This half runs on your machine; that half runs on the
server, and the two speak over HTTPS.

Submissions go to **`survey.agentbenchmark.dev`** and nowhere else. That is a
constant in the source, not a setting: there is no local survey to run here, and
a result that never left your machine would not be comparable with anyone
else's, which is the entire point of the exercise. You need a token to submit;
ask whoever pointed you here.

## Install

```bash
python3 ami-survey/scripts/install.py --user
```

It asks for your submission token, and that is the only thing to supply. Then
restart your agent. `GETTING-STARTED.md` has the Windows form, the Codex form,
and what to do when it does not work.

## Commands

In `ami-survey/bin/`. Each is a wrapper that sets `PYTHONPATH` and runs a module,
so they work from a clone with nothing installed. `--help` on any of them prints
its full flags.

### Benchmarking a workflow across models

**`ami-run`** runs a workflow against a provider's API — your key, your account —
reads the `usage` block off every real response, and submits the survey. One
agent loop over four sandboxed file tools, identical for every provider, which is
what makes the numbers comparable between them.

```bash
export OPENAI_API_KEY=...

# see what would run, without calling anything
ami-survey/bin/ami-run support-ticket-triage --provider openai --model gpt-4.1 --dry-run

# the real thing
ami-survey/bin/ami-run support-ticket-triage --provider openai    --model gpt-4.1
ami-survey/bin/ami-run support-ticket-triage --provider anthropic --model claude-sonnet-4-5
ami-survey/bin/ami-run support-ticket-triage --provider gemini    --model gemini-2.5-pro

# a workflow someone sent you, kept wherever you put it
ami-survey/bin/ami-run their-workflow --dir ~/Downloads/handover \
    --provider openai --model gpt-4.1

# any OpenAI-compatible endpoint, including a local model
ami-survey/bin/ami-run support-ticket-triage --provider openai --model llama3.3 \
    --base-url http://localhost:11434/v1 --api-key-env OLLAMA_API_KEY
```

Always `--dry-run` first with a workflow you did not write. It resolves the
prompt, shows the sandboxed workspace, names any `---` blocks it is *not*
sending, and calls nothing.

Worth knowing about `--grade`: the default is `auto`, which has the model grade
its own output. That is the one field in the survey nothing can check, so for
anything you intend to stand behind, use `--grade skip` and submit your own
grade afterwards with `--no-reset`.

### Managing workflows

**`ami-workflow`** lists, scaffolds and prepares them. It never runs anything —
that is `ami-run`, and they are separate commands so that printing a prompt and
spending money on an API are not one word apart.

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

Reading the collected results is not here: submissions live on the survey
service, and the dashboard there is where runs are compared.

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
