# Let an agent adapt your workflow for you

## Just ask

Once the survey is installed, say this to your agent and paste your prompt when
it asks:

> Could you rewrite my workflow so I can take the AMI survey?

It will hand back the adapted prompt, a `workflow.json`, and a short note of what
it changed. Nothing to copy from this page.

Other phrasings that work: *"make this prompt measurable"*, *"adapt this
workflow for benchmarking"*, *"prepare this prompt for the AMI survey"*.

## If your agent does not pick it up

Some agents do not support skills, or may not connect the request to the right
one. Copy everything in the box below and paste it along with your prompt — it
is the same instructions, given directly.

---

```
I want to benchmark one of my existing agent workflows using the AMI survey, and
I need you to adapt my prompt so the run can be measured. I will paste my prompt
below.

Your job is to ADD measurement scaffolding, not to improve my prompt. Do not
reword my instructions, do not add requirements I did not ask for, and do not
change what the workflow actually does. If you think the prompt has problems,
tell me separately — do not fix them silently.

Make exactly these changes:

1. STAGES. Identify the natural phases of the work — the points where the agent
   switches from one kind of activity to another, like research→writing or
   classify→respond. Most workflows have two or three. Add one line near the end
   of my prompt:

       Call `ami_mark_stage` as you move between the phases of this work, using
       the stage names "<first>" and "<second>".

   Name them using MY vocabulary from MY prompt, not generic labels like
   "Phase 1". If the workflow genuinely has only one phase, say so and skip this
   change rather than inventing a split — a fake boundary produces a fake
   breakdown.

2. STOP LINE. Add this as the very last line, exactly as written:

       When you are finished, stop. Do not take the survey yet.

3. STANDARD. Check whether my prompt already says how to tell if the output is
   good — a template, a checklist, a set of rules, a required format. If it
   does, leave it alone. If it does not, do NOT invent one: tell me it is
   missing, explain that the quality grade will be meaningless without it, and
   suggest in one sentence what a standard for this task might look like. Let me
   decide.

Then produce, in this order:

A. The rewritten prompt, in a code block, ready to copy. It must be my original
   text with only the additions above.

B. A `workflow.json` file, in a code block, like this:

       {
         "workflow_name": "<short, stable name for this job>",
         "workflow_description": "<1-3 sentences: what goes in, what comes out>"
       }

   The name is how repeated runs are grouped, so it must be something that would
   not change between runs. Name the JOB, not this instance of it: "Invoice
   Processing", not "Invoice Processing March 2026".

C. A short list of what you changed and why, in plain language, so I can check
   it. Include any concerns from step 3 here.

Do not run the workflow. Do not take the survey. Only produce A, B and C.

My prompt follows:
---
[PASTE YOUR PROMPT HERE]
```

---

## What to do with the result

1. Save the rewritten prompt wherever you keep your prompt.
2. Save the `workflow.json` next to it.
3. Run it — the two-message rule from
   [GETTING-STARTED.md](GETTING-STARTED.md#the-two-message-rule) still applies:
   your prompt first, then *"Take the AMI survey regarding &lt;workflow&gt;"* as a
   separate second message.

## Check it before you trust it

Read section C and confirm the agent only added things. The most common mistake
is a helpful agent "improving" your instructions along the way, which changes
what you're measuring — and a benchmark of a workflow you don't actually run is
worse than no benchmark.

If it tells you your workflow has no objective standard, that's worth taking
seriously rather than working around. It means the quality grade will be the
agent's opinion of its own work with nothing to check it against. The cost and
speed numbers are still perfectly good; it's only the grade that becomes noise.
