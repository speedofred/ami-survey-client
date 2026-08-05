---
name: ami-make-measurable
description: Adapt an existing workflow prompt so the run can be measured by the AMI survey - adds stage markers, a stop line and a stable workflow name, and produces the workflow.json that groups repeat runs. Use when asked to "rewrite my workflow so I can take the AMI survey", "make this prompt measurable", "adapt this workflow for benchmarking", or "prepare this prompt for the AMI survey". This is preparation done BEFORE the work runs; taking the survey afterwards is the separate ami-survey skill.
---

# Make a workflow measurable

Someone has a prompt they already use and wants to benchmark it. Your job is to
add the measurement scaffolding, hand it back, and stop.

**Add, do not improve.** Their prompt is the thing being measured. Rewording an
instruction, tightening a vague requirement or fixing what looks like an
oversight all change what the benchmark measures, and a benchmark of a workflow
they do not actually run is worse than no benchmark. If you see a genuine
problem, say so in your summary and let them decide.

**Do not run the workflow, and do not take the survey.** Produce the three
outputs below and nothing else.

## First, find the prompt

If they have not given you the prompt yet, ask for it. If they pointed at a
file, read it. Do not guess at what their workflow does from its name.

## The three changes

### 1. Stage markers

Identify the natural phases — the points where the work changes character, like
research→writing, classify→respond, extract→check. Most workflows have two or
three. Add one line near the end of their prompt:

```
Call `ami_mark_stage` as you move between the phases of this work, using the
stage names "<first>" and "<second>".
```

Name stages in **their** vocabulary, taken from their prompt. "Screen CVs" not
"Phase 1". The names appear in the results, and generic labels make the
breakdown useless.

If the workflow genuinely has one phase, say so and skip this change. An
invented boundary produces an invented breakdown.

### 2. The stop line

Add this as the very last line, exactly:

```
When you are finished, stop. Do not take the survey yet.
```

Measurement ends at the human turn that asks for the survey. Without this, an
agent surveys itself in the same turn as the work — measuring a run that has not
finished, and charging the survey's own tokens to the workflow.

### 3. A standard to grade against

Check whether the prompt already says how to judge the output: a template, a
checklist, rules, a required format, a right answer.

- **If it does**, leave it alone.
- **If it does not**, do **not** invent one. Say it is missing, explain that the
  quality grade will be the agent's unchecked opinion of its own work, and
  suggest in one sentence what a standard might look like. It is their call.

Inventing a rubric is the worst option available: it makes the grade look
meaningful when it measures nothing.

## What to produce

**A. The rewritten prompt**, in a code block, ready to copy. Their original text
with only the additions above.

**B. `workflow.json`**, in a code block:

```json
{
  "workflow_name": "Invoice Processing",
  "workflow_description": "Reads a batch of supplier invoices, extracts totals and dates, and flags anything failing the approval rules."
}
```

Runs group by `workflow_name`, so it must be stable across runs. Name the job,
not the instance: "Invoice Processing", never "Invoice Processing March 2026".
The description is 1–3 sentences: what goes in, what comes out.

Offer to write both files if you can see where they belong.

**C. A short summary** of what you changed and why, in plain language, so they
can check it — including any concern from change 3.

## Then tell them how to run it

Two messages, not one:

1. The rewritten prompt, in a fresh session.
2. Once it stops: *"Take the AMI survey regarding &lt;workflow name&gt;."*

Sending them together measures an unfinished run.

## Failure modes to avoid

- **Improving the prompt.** The most likely mistake, because it feels helpful.
  Every reworded instruction changes the thing being measured.
- **Inventing a quality standard** where the workflow has none.
- **Splitting single-phase work** into stages that do not exist.
- **Running the workflow** to "check it works". You are preparing it, not
  executing it, and a trial run pollutes the session the survey will measure.
