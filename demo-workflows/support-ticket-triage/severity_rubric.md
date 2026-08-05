# Severity rubric — Northwind Cloud support

Assign exactly one severity per ticket. When a ticket satisfies criteria at more
than one level, **assign the highest level it satisfies.**

## P1 — Critical

Any one of:
- Core service unavailable or erroring for all or most users of an account.
- Confirmed or suspected exposure of one customer's data to another party.
- Security incident, credential compromise, or suspected breach.
- Complete data loss with no known recovery path.

SLA: first human response 30 minutes, hourly updates, incident channel opened.

## P2 — High

Any one of:
- Major feature broken or severely degraded for a group of users, with no
  workaround.
- Financial impact on the customer (incorrect billing, failed payouts) above £500.
- Material brand or reputational risk: public complaint, threat to escalate
  publicly, regulator or press mentioned.
- Enterprise-tier account blocked from a contracted workflow.

SLA: first human response 2 business hours, daily updates.

## P3 — Medium

Any one of:
- Feature broken or degraded for a single user, or for a group with a viable
  workaround.
- Financial impact at or below £500.
- Confusing, incorrect, or missing behaviour that does not block the user's work.

SLA: first human response 1 business day.

## P4 — Low

Any one of:
- Feature request, enhancement, or product feedback.
- Cosmetic issue with no functional impact.
- Question answerable from documentation.

SLA: first human response 3 business days.

## Notes for the classifier

- Severity reflects **impact and risk**, not the customer's tone. An angry
  message about a cosmetic bug is still P4; a calm message describing a data leak
  is still P1.
- Account tier raises severity only where the rubric says so explicitly.
- If the ticket describes a symptom whose plausible cause would be a higher
  severity, classify on the plausible cause and say so in your reasoning.
