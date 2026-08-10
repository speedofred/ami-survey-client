# Benchmarking your own agent workflow

A guide for measuring what an AI agent actually costs you when it does a real
piece of work — and for turning one of your existing prompts into something
measurable.

No prior knowledge assumed. Roughly 15 minutes to set up, then 5 minutes per
workflow you want to measure.

---

## What this is, in plain terms

When you ask an agent to do a job, you get the finished work — but no idea what
it cost. How many times did it call the model? How many tokens? How long? How
much money? Was the output any good?

This tool answers those questions **by reading the agent's own records**, not by
asking the agent to remember. That distinction is the whole point: an agent
genuinely does not know its own token counts, and if you ask it, it will guess.
A guessed benchmark is worse than no benchmark, because it looks like data.

So the rule throughout is simple: **every number is measured, never recalled.**

What you end up with, per run:

| | |
|---|---|
| Cost | tokens in and out, and what that cost in dollars |
| Speed | how long the agent spent working |
| Effort | where the tokens went, phase by phase |
| Quality | a graded judgement of the output |

Run the same workflow twice and you can compare. Run it on two different models
and you can choose between them with numbers instead of impressions.

---

## Before you start

You need three things:

1. **An AI coding agent on your computer** — Claude Code or Codex. This works
   with what you already use; you don't install a new assistant.
2. **Nothing else.** The setup gets its own submission token when you run it —
   press Enter at the prompt and it registers one for you. If somebody has
   already sent you a token, paste that instead; treat it like a password.
3. **Python 3.9 or newer.**
   - **macOS** — already installed. Nothing to do.
   - **Windows** — install it from [python.org](https://www.python.org/downloads/),
     and **tick "Add python.exe to PATH"** on the first screen of the installer.
     Do not install it from the Microsoft Store; the Store version behaves
     oddly when other programs try to launch it.
   - **Linux** — `sudo apt install python3 git` or your distribution's equivalent.

> **A note on the terminal.** A few steps need you to type commands. On macOS
> that's **Terminal**; on Windows, **PowerShell** (press Start and type
> "PowerShell"). You can copy and paste every command. If a command produces no
> output, that usually means it worked.
>
> Throughout, where macOS says `python3`, **Windows says `python`**. That one
> substitution is the only difference in the commands below.

---

## Part 1 — Set it up

### Step 1: Download it

In a terminal, paste this:

**macOS or Linux:**

```bash
git clone https://github.com/speedofred/ami-survey-client ~/ami-survey
cd ~/ami-survey
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/speedofred/ami-survey-client $HOME\ami-survey
cd $HOME\ami-survey
```

This puts the tool in a folder called `ami-survey` in your home directory. If
Windows says `git` is not recognised, install [Git for
Windows](https://git-scm.com/download/win) first and reopen PowerShell.

### Step 2: Connect it to your agent

Run **one** of these — whichever agent you use.

**macOS or Linux:**

```bash
python3 ami-survey/scripts/install.py --user    # Claude Code
python3 ami-survey/scripts/install.py --codex   # Codex
```

**Windows (PowerShell):**

```powershell
python ami-survey\scripts\install.py --user    # Claude Code
python ami-survey\scripts\install.py --codex   # Codex
```

It will ask for a submission token:

- **Don't have one?** Press Enter. It registers one for you, asks what to call
  it, and writes it into your agent's configuration. Nothing else to do.
- **Been sent one?** Paste it. It **won't appear on screen as you type**, which
  is intentional, so it isn't left in your terminal history.

The token it registers is shown once. Keep a copy if you ever want to reinstall
without registering again — but nothing breaks if you don't, you just register
another.

There is nothing to configure beyond that. Surveys go to
`survey.agentbenchmark.dev`; the tool has no other destination, so there is no
setting to get wrong and no way to end up measuring into a void.

Codex will print a short block of configuration and ask you to paste it into a
file. Follow what it says on screen.

### Step 3: Restart your agent

**Fully quit** Claude Code or Codex and open it again. Closing the window isn't
enough — on macOS use Cmd-Q or quit from the menu; on Windows use Alt+F4, or
right-click the taskbar icon and choose Close.

This matters because the settings are only read when the app starts. Skipping
this step is the single most common reason the next part appears to do nothing.

### Step 4: Check it worked

Open your agent and ask it, in plain English:

> Fetch the AMI survey instructions and tell me in one sentence what the survey measures.

If it comes back with a sensible answer about tokens, cost and workflow stages,
you're connected. If it says it has no such tool, go back to Step 3 — the restart
is almost always the culprit.

---

## Part 2 — Turn your workflow into a benchmark

You have a prompt you already use. Ask your agent to adapt it:

> **Could you rewrite my workflow so I can take the AMI survey?**

Paste your prompt when it asks. You get back three things: the rewritten prompt,
a small `workflow.json` file, and a note of what it changed and why.

That's the whole step. The rest of this section explains what it should have
done — worth the two minutes, because **you** are the one who knows what your
workflow is supposed to do, and the checks below are quick.

If your agent doesn't offer it, [MAKE-IT-MEASURABLE.md](MAKE-IT-MEASURABLE.md)
has the same instructions to paste in by hand.

### Checking its work

Read the summary it gives you and confirm three things.

**It only added; it didn't improve.** Your instructions should be word for word
what they were. A helpful agent tidying your prompt sounds harmless and isn't —
you'd be benchmarking a workflow you don't actually run.

**The stage names are yours.** They should read like your own description of the
work — "Screen CVs", "Draft Emails" — not "Phase 1" and "Phase 2". These names
appear in your results, and generic ones make the breakdown useless.

**It told you if something was missing.** If your prompt has no way to judge
whether the output is good, the agent should have *said so* rather than inventing
a standard. That's a real finding, not a failure — see below.

### What makes a workflow worth measuring

Three things. Miss any one and you get numbers that don't mean much:

**1. A job with a real output.** Something that produces files, drafts, or
decisions you could show someone. "Summarise this" is measurable; "what do you
think about X" is not.

**2. A way to tell whether the output is good.** A checklist, a template, a set
of rules, a right answer. Without this the quality grade is just a feeling, and
the most interesting column in your results becomes noise.

**3. Two or more natural stages.** Most real work has them — *research, then
write*; *classify, then respond*; *find the bug, then fix it*. Stages are what
let you see where the money actually went, which is usually not where you'd
guess.

### Doing it by hand

You don't need this if you asked your agent — it's here so you know exactly what
changed, and for anyone who'd rather do it themselves. Four edits; nothing else
changes.

#### Change 1: Name the stages

Find the natural phases in your prompt and add a line naming them:

```
Call `ami_mark_stage` as you move between the two phases of this work, using the
stage names "Research" and "Draft".
```

Use your own words for the stage names — whatever you'd call those phases when
describing the job to a colleague. Generic names like "Phase 1" tell you nothing
later.

#### Change 2: Tell it to stop

Add this as the last line of your prompt:

```
When you are finished, stop. Do not take the survey yet.
```

This is more important than it looks, and the reason is worth understanding:
measurement stops when *you* ask for the survey. If the agent surveys itself in
the same breath as doing the work, it measures a job that hasn't finished and
bills the survey's own cost to your workflow.

#### Change 3: Say what "good" looks like

If your prompt doesn't already, point at the standard the output should meet:

```
Each reply must contain every element listed in `response_template.md`.
```

or

```
Every classification must cite the rule from `rules.md` that it applied.
```

The agent grades its own work at the end. Given something concrete to grade
against, that judgement is worth reading. Given nothing, it will say "A".

#### Change 4: Pin the name (optional but recommended)

Results are grouped by workflow name. If the agent invents a slightly different
name each run — "Invoice Processing" then "Invoice Processing Workflow" — your
runs won't group and you can't compare them.

Create a small file called `workflow.json` next to your prompt:

```json
{
  "workflow_name": "Invoice Processing",
  "workflow_description": "Reads a batch of supplier invoices, extracts totals and dates, and flags anything that fails the approval rules."
}
```

Then, when you ask for the survey, add: *"Use the workflow_name and
workflow_description from workflow.json."*

### A worked example

**Before** — a prompt that works fine but can't be measured:

```
Go through the invoices in invoices/ and pull out the total, the date and the
supplier for each one. Flag any that break the rules in approval-rules.md.
Write the results to output/summary.csv.
```

**After** — the same job, now measurable:

```
Go through the invoices in invoices/ and pull out the total, the date and the
supplier for each one. Flag any that break the rules in approval-rules.md.
Write the results to output/summary.csv.

Every flagged invoice must cite the specific rule from approval-rules.md that
it breaks.

Call `ami_mark_stage` as you move between the two phases of this work, using the
stage names "Extract" and "Check Rules".

When you are finished, stop. Do not take the survey yet.
```

Three added lines. The job is identical; it is now something you can compare.

---

## Part 3 — Run it

### The two-message rule

This is the only part of the process that's easy to get wrong.

**Message 1** — your adapted prompt. Let the agent work until it stops.

**Message 2** — sent separately, after it has finished:

> Take the AMI survey regarding the invoice processing workflow.

Two messages, not one. Sending them together means the agent measures itself
while still working, and the numbers include the survey's own cost.

Start each run in a **fresh conversation**, not a continuation of an old one.
Measurement begins at the first thing you said, so a conversation with unrelated
chat at the top will over-count.

### What good output looks like

The agent will report something like:

```
survey submitted: run 3543e9e0ef72
  model            gpt-5.6-terra (openai)
  API calls        18
  tokens           511,308 in / 6,002 out
  agent runtime    127.5 s
  est. cost        $0.2422
  grade            A (self)
```

**Read the warnings if there are any.** They're limits on how much the data can
be trusted — an unresolved price, a stage that couldn't be placed. No warnings
means a clean measurement.

### Sanity-check the grade

The agent grades its own work, which is the weakest number in the set. Open what
it produced and see whether you agree. If you don't, that disagreement is the
most interesting thing the run told you.

---

## What leaves your computer

You're sending data to someone else's server, so here is exactly what that is.

**Sent:** token counts, timings, the model name and its price, which tools were
used (by name), the workflow name and description you wrote, and the grade with
its justification.

**Not sent:** your files, your prompts, your agent's replies, the contents of
anything it read or wrote, or the commands it ran. The shell commands your agent
executed are stripped before storage, as are file paths, your username and your
session identifiers.

The measurement happens on your machine. Only the finished summary travels, and
it travels to one place: `survey.agentbenchmark.dev`, over HTTPS. This tool does
not keep a copy for you — the survey is the shared benchmark, not a private
report — and you can read back your own submissions, and only your own, through
the links it prints when you submit.

---

## Removing it

One command, whenever you like:

```bash
python3 ami-survey/scripts/uninstall.py      # macOS / Linux
python ami-survey\scripts\uninstall.py       # Windows
```

It removes the skill and takes the survey out of your agent's settings, leaving
any other tools you have configured exactly as they were. It prints everything
it removed, and makes a backup of each settings file first.

Surveys stored on your own computer are **kept** by default and their location
is printed. To delete those too:

```bash
python3 ami-survey/scripts/uninstall.py --purge
```

If you use Codex, it will remind you to delete one block from
`~/.codex/config.toml` by hand — that file is left alone automatically because
it can hold credentials for other things.

Then fully quit and reopen your agent. Finally, delete the downloaded folder
(`~/ami-survey`) if you want it gone entirely — the uninstaller tells you where
it is rather than deleting the ground it is standing on.

## If something goes wrong

**"I don't have that tool" / nothing happens**
The restart. Fully quit the app — Cmd-Q on macOS, Alt+F4 on Windows, not just
closing the window — and reopen. Settings are read only at launch.

**"Unrecognised token"**
The token is wrong, expired, or was revoked. Check for a stray space when you
pasted it, then ask for a new one.

**It says it submitted, but nothing arrives**
Ask whoever gave you the token to check. Submissions have exactly one
destination, so a survey that reported success reached the server — but a
revoked token or a rejected submission will say so rather than succeed
quietly.

**Windows: "python is not recognised"**
Python was installed without being added to PATH. Re-run the python.org
installer, choose *Modify*, and tick **"Add python.exe to PATH"**. Then close
PowerShell and open a new one.

**The agent took the survey without being asked**
Your prompt is missing *"When you are finished, stop. Do not take the survey
yet."* Add it and run again. Also check you aren't pasting a **command** for the
agent to run — if it runs something that prints instructions, it will follow
them.

**"No stages were declared" in the warnings**
The `ami_mark_stage` line is missing from your prompt, or the agent ignored it.
The run is still measured; you just lose the phase-by-phase breakdown.

**The cost shows as unavailable**
The model you used isn't in the public price list. Everything else is still
valid — only the money column is missing.

---

## Comparing runs

Once you have a few, the point becomes visible:

```
Submitted            Model            Calls   In tok    Out tok   Cost $  Grade
2026-08-03 15:23:11  claude-opus-5       17  729,874     10,432   0.7954      B
2026-08-04 19:51:19  gpt-5.6-terra       18  511,308      6,002   0.2422      A
```

Same job, two models, one third of the cost. That's the question this answers —
and now it's a measurement rather than a hunch.

One honest caveat: the grade column is each agent's opinion of its own work.
Cost, tokens and timing are measured and directly comparable. Grades are not,
until a human checks them.
