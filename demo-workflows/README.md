# Demo workflows

Workflows built to be benchmarked with the AMI survey. Each one is a realistic
piece of business work with an objective standard to grade against, so the
resulting survey response measures something rather than reflecting a mood.

## support-ticket-triage

Six inbound support tickets for a fictional SaaS company. The agent classifies
each ticket's severity against a rubric, then drafts a customer reply against a
template. Two natural stages, so the Agent Effort Profile has something to split.

| File | Purpose |
|---|---|
| `support-ticket-triage/PROMPT.md` | the exact prompt to paste |
| `support-ticket-triage/tickets/` | the six inbound tickets |
| `support-ticket-triage/severity_rubric.md` | P1–P4 definitions and SLAs |
| `support-ticket-triage/response_template.md` | the six required reply elements |
| `support-ticket-triage/output/` | where the agent writes its results |
| `support-ticket-triage/workflow.json` | the canonical `workflow_name`/`workflow_description` every run groups by |
| _(answer key)_ | not shipped with the demo — it names the expected severity for every ticket, and the demo is only worth running against an agent that has not seen it |

### Running it

Start each run from a clean slate:

```bash
ami-survey/bin/ami-demo support-ticket-triage
```

That clears the demo's `output/`, drops any half-finished survey state left by an
interrupted run, prunes unsubmitted drafts, and prints both messages to send —
the workflow prompt and the follow-up that closes the measurement window.
Submitted responses are never touched; they are the benchmark. `ami-demo` with no
argument lists the demos and how many surveys each already has.

Then:

1. Open a **fresh** session at the repository root — a new conversation in the
   app, not a continuation. This matters because the measurement window opens at
   the first human turn, so a session with unrelated work in it over-counts.
   Confirm with:

   ```bash
   ami-survey/bin/ami-session
   ```

   A fresh session shows 1 human turn and a window starting moments ago. Don't run
   two sessions in this directory at once during a benchmark — the survey measures
   whichever was written most recently.
2. Paste the workflow prompt from `PROMPT.md`. Let the agent finish.
3. Send a **separate second message**: `Take the AMI survey regarding the support
   ticket triage workflow.` Sending it separately closes the measurement window
   at that turn, keeping the survey's own token spend out of the workflow's figures.
4. Grade honestly. The agent grades itself by default; open the answer key and
   check it. If you disagree, that disagreement is the interesting finding.

### What the tickets are testing

Three are unambiguous (TCK-1001 outage, TCK-1003 feature request, TCK-1002 single
user). Three separate a careful agent from a fast one:

- **TCK-1004** — an angry customer with a £4,207 overcharge and a threat to go
  public. Tests whether severity tracks impact rather than tone.
- **TCK-1005** — degraded performance for a group, but with a workaround. Tests
  whether the agent reads the rubric's "no workaround" qualifier or pattern-matches
  on "group of users".
- **TCK-1006** — reported by the customer as "probably a small display bug", but
  the described symptom is one practice seeing another practice's client names and
  phone numbers. A data exposure. This is the ticket worth watching: an agent that
  takes the customer's framing at face value calls it P3 and tells them not to
  worry.

### Comparing runs

One survey is a measurement. Several of the same workflow are a benchmark:

```bash
ami-survey/bin/ami-compare "Support Ticket Triage"
```

Run the same prompt more than once, or with a different model, prompt phrasing, or
with and without stage markers, then compare cost, speed and grade together. The
per-stage breakdown shows where the tokens actually went.

Reset between runs with `ami-survey/bin/ami-demo <name>`. Survey responses
accumulate in `ami-survey/data/responses/` and are meant to.

### Running it in Codex

Same two messages, different agent. Install once:

```bash
ami-survey/scripts/install.sh --codex     # symlinks the skill into ~/.codex/skills
```

then add the printed `[mcp_servers.ami-survey]` block to `~/.codex/config.toml`
and restart Codex. After that the flow is identical to Claude Code — reset with
`ami-demo`, paste the prompt, then send *"Take the AMI survey regarding the
support ticket triage workflow"* as a separate second message. The survey works
out that it is in Codex, reads that session's rollout for token usage, and prices
the model it actually ran on. Neither you nor the agent supplies anything else.

`ami-survey/bin/ami-session` shows which session would be measured, across both
runtimes. Don't leave a Claude Code and a Codex session both working in this
directory at once during a benchmark — the newest log wins.

### Running it on a model with no agent harness

The same prompt, the same survey, a different model:

```bash
export OPENAI_API_KEY=sk-...
ami-survey/bin/ami-run support-ticket-triage --provider openai    --model gpt-4.1
ami-survey/bin/ami-run support-ticket-triage --provider anthropic --model claude-sonnet-4-5
ami-survey/bin/ami-run support-ticket-triage --provider gemini    --model gemini-2.5-pro
```

The runner drives an agent loop over `read_file` / `write_file` / `list_files` /
`mark_stage`, records the usage block of every real response, and submits the
survey — so a GPT run, a Gemini run and a Claude Code run land in the same
`ami-compare` table. Because all of them read `workflow.json` for the workflow
name, they group together automatically.

```bash
ami-survey/bin/ami-run support-ticket-triage --provider gemini --dry-run       # no API call
ami-survey/bin/ami-run support-ticket-triage --provider openai --model gpt-5 \
    --grade skip                                                               # grade it yourself
ami-survey/bin/ami-run support-ticket-triage --provider openai --model llama3.3 \
    --base-url http://localhost:11434/v1 --api-key-env OLLAMA_API_KEY          # local model
```

With `--grade auto` (the default) the model grades its own output against the AMI
scale in one extra call, which is deliberately excluded from the telemetry — the
same reason the survey turn is excluded from a Claude Code run. Check any
self-grade against the answer key; a model grading itself is the weakest number in
the table.

Two caveats when comparing across runtimes:

- `total_agent_runtime` for a runner run is client-observed request latency, while
  a Claude Code run measures from the tool result that triggered each request.
  Tokens, call counts and cost compare directly; treat runtime as indicative.
- The runner's four file tools are not Claude Code's toolset. You are comparing
  models at the same task, not harnesses at the same task — a weaker result may be
  the tools, not the model.

## Adding your own

The survey does not care what the workflow is. To make one benchmarkable it needs
three things:

1. **A stable name** — runs group by `workflow_name`, so drift makes them
   incomparable.
2. **An objective standard** — a rubric, a spec, an answer key. Without one the
   grade is a vibe and the quality column is noise.
3. **Two or more natural stages** — so the effort profile shows where the cost
   went, not just what it was.
