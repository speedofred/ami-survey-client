# The workflow prompt

Reset first — `ami-survey/bin/ami-demo support-ticket-triage` clears the output
and prints both messages below. Then paste everything between the lines into a
**fresh** agent session opened at the repository root.

---

You are handling the inbound support queue for Northwind Cloud.

Working in `demo-workflows/support-ticket-triage/`:

1. Read every ticket in `tickets/`.
2. For each ticket, classify its severity against `severity_rubric.md`.
3. For each ticket, draft a reply to the customer that satisfies every
   requirement in `response_template.md`.

Produce two outputs:

- `output/triage.json` — an array of objects, one per ticket, each with
  `ticket_id`, `severity`, `reasoning` (2–3 sentences citing the rubric clause
  you applied), and `routed_to` (the owning team).
- `output/replies/<ticket_id>.md` — the drafted reply for each ticket.

Call `ami_mark_stage` as you move between the two phases of this work, using the
stage names "Classify Severity" and "Draft Replies".

When you are finished, stop. Do not take the survey yet.

---

Then, in the same session, send a second message:

---

Take the AMI survey regarding the support ticket triage workflow. Use the
workflow_name and workflow_description from `workflow.json` so this run groups
with the others.

---

## Why it is split into two messages

The measurement window closes at the human turn that requests the survey, so
sending it separately keeps the survey's own token spend out of the workflow's
figures. If you ask for both in one message, the window stays open to the present
and the survey measures itself as well.
