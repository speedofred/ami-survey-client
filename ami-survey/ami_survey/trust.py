"""How far a submission's numbers can be believed, and why.

Two things live here, and they answer different questions.

**The tier** answers *where did these numbers come from?* Either an adapter read
them out of the runtime's own session log - which the submitter did not write and
cannot edit without effort - or the submitting agent typed them into a request.
The first is `measured`, the second is `reported`. That distinction is the whole
premise of the benchmark, so it is a field, not a footnote, and it is derived
from the adapter label rather than from anything the submitter asserts.

There is a third answer, and it is *nowhere*: a run with no call records at all.
Some surfaces cannot produce them - a browser agent reached over MCP has no
access to its own per-call token counts, so an honest submission from one has a
workflow name, a description and a grade, and no measurements whatsoever. That is
worth keeping, and it is not a `reported` run: nobody reported anything. Filing
it as `reported` would put it in the same bucket as an agent that read forty real
call records off its runtime, which is the exact confusion the tier exists to
prevent. So it is `unmeasured`, and one filter drops the lot.

**The plausibility checks** answer *could these numbers be true?* They are only
meaningful for `reported` runs; a measured one was not typed by anyone. They are
mechanical and cheap: monotonic time, a physical ceiling on tokens per second,
call durations that fit inside the window they claim.

The checks warn; they never reject. A rejection tells whoever submitted exactly
where the threshold sits and invites them to tune the claim to sit just under it.
A stored run with a failed check is evidence, and evidence is what a benchmark is
for. Nothing here makes self-reported numbers trustworthy - it makes them
*auditable*, and separable from the ones nobody had the opportunity to invent.
"""

from __future__ import annotations

from .timeutil import parse_ts

MEASURED = "measured"
REPORTED = "reported"
UNMEASURED = "unmeasured"

#: Adapter labels that read a runtime's own records. Anything not named here is
#: taken on the submitter's word, including labels invented by a caller - the
#: default has to be the untrusting one.
MEASURED_ADAPTERS = frozenset({"claude_code_transcript", "codex_rollout"})

#: Prefix-matched adapter labels, for adapters whose label carries a variant.
#: Empty since `ami-runner/` was removed with the runner it named: a label that
#: grants the measured tier for a collector this project no longer ships is a
#: claim nothing can ever corroborate.
MEASURED_ADAPTER_PREFIXES: tuple[str, ...] = ()

TIER_NOTE = {
    MEASURED: (
        "Read from the runtime's own session log by an AMI adapter. The submitting "
        "agent did not supply these numbers and could not have."
    ),
    REPORTED: (
        "Supplied by the submitting agent over the API. Nothing read the runtime's "
        "records, so these numbers are a claim - see the plausibility checks."
    ),
    UNMEASURED: (
        "No call records were submitted, so the token, call-count and price fields "
        "are empty. Any timings present were observed by this server as the run's "
        "stage markers arrived, not reported by the agent - they measure elapsed "
        "time, which is not the same thing as work done."
    ),
}


def tier_for(adapter: str | None, calls: list | None = None) -> str:
    """Where a run's numbers came from.

    `calls` is optional so callers that only hold an adapter label keep working.
    Pass it when you have it: a run with none is `unmeasured` whatever adapter it
    claims, because there is nothing for the adapter to have read.
    """
    if calls is not None and not calls:
        return UNMEASURED
    label = adapter or ""
    if label in MEASURED_ADAPTERS or label.startswith(MEASURED_ADAPTER_PREFIXES):
        return MEASURED
    return REPORTED


COMPARABLE = {
    MEASURED: "other measured runs",
    REPORTED: "other reported runs only - do not rank against measured runs",
    UNMEASURED: "nothing - there are no measurements here to compare",
}


#: What the tier rests on, and whether anything backs it up.
#:
#: The tier is derived from the adapter label. The label arrives in a request.
#: For a token this server issued to someone it knows, that is a reasonable thing
#: to take at face value - the operator vetted the submitter. For a token minted
#: by a stranger with nobody in the loop, it is not: an adapter that really read a
#: transcript and a caller who typed the words `claude_code_transcript` are
#: indistinguishable from here.
#:
#: Recording that as a separate fact rather than folding it into the tier keeps
#: both true. The tier still says where the numbers claim to come from; this says
#: whether anyone could check.
CORROBORATION = {
    True: (
        "Submitted with a token issued directly by the operator, so the adapter "
        "label came from a known submitter."
    ),
    False: (
        "Submitted with a self-issued token, which this server did not vet. The "
        "adapter label is therefore uncorroborated: nothing here distinguishes an "
        "adapter that read a real runtime log from a caller who typed its name."
    ),
}


def tier_block(adapter: str | None, calls: list | None = None,
               corroborated: bool = True) -> dict:
    tier = tier_for(adapter, calls)
    corroborated = bool(corroborated)
    return {
        "tier": tier,
        "adapter": adapter or "unknown",
        "why": TIER_NOTE[tier],
        "comparable_with": COMPARABLE[tier],
        # Who submitted this, always - not only where the tier makes a claim.
        # Forcing it true for unmeasured runs seemed harmless when an unmeasured
        # run carried nothing but a grade; it meant a stranger's submission was
        # displayed as vouched for, which nobody had done. Unmeasured runs also
        # carry observed stage timings now, so "nothing to corroborate" was not
        # even true any more.
        "corroborated": corroborated,
        "corroboration": CORROBORATION[corroborated],
    }


# --------------------------------------------------------------------------- #
# plausibility
# --------------------------------------------------------------------------- #

#: Output tokens per second, above which a claim is not physically credible.
#: Deliberately generous - roughly an order of magnitude above the fastest
#: production decoding rates - because the job is to catch fabrication, not to
#: adjudicate benchmarks. A real run has never come close.
MAX_OUTPUT_TOKENS_PER_SECOND = 2000

#: A single call claiming more than this is likelier to be a whole session
#: reported as one request than a genuine call.
MAX_TOKENS_PER_CALL = 5_000_000


def _seconds(call: dict) -> float | None:
    try:
        start, end = parse_ts(call["start_time"]), parse_ts(call["end_time"])
    except (KeyError, ValueError, TypeError):
        return None
    return (end - start).total_seconds()


def check(calls: list[dict], fields: dict, adapter: str | None) -> dict:
    """Run every check. Returns the block that goes on the response.

    Measured runs are checked too, and are expected to pass: a failure there is
    a bug in an adapter rather than a dishonest submitter, and finding one that
    way is worth more than skipping the work.
    """
    if not calls:
        # Vacuous passes on an empty run would read as a clean bill of health for
        # a submission containing nothing to check.
        return {
            "tier": tier_for(adapter, calls),
            "checks": [],
            "failed": [],
            "note": (
                "No call records were submitted, so there is nothing to check. This "
                "is not a pass: an unmeasured run has no numbers to be true or false."
            ),
        }

    results = [
        _time_is_ordered(calls),
        _decoding_rate_is_possible(calls),
        _calls_fit_their_window(calls, fields),
        _no_single_call_holds_a_session(calls),
        _totals_match_the_calls(calls, fields),
    ]
    failed = [r["check"] for r in results if r["status"] == "fail"]
    return {
        "tier": tier_for(adapter, calls),
        "checks": results,
        "failed": failed,
        "note": (
            "Mechanical checks on whether the reported numbers could be true. They "
            "do not verify that they are. A failure is recorded, never rejected: "
            "the run stays in the dataset, flagged."
        ),
    }


def _ok(name: str, detail: str) -> dict:
    return {"check": name, "status": "pass", "detail": detail}


def _fail(name: str, detail: str) -> dict:
    return {"check": name, "status": "fail", "detail": detail}


def _skip(name: str, detail: str) -> dict:
    return {"check": name, "status": "not_applicable", "detail": detail}


def _time_is_ordered(calls: list[dict]) -> str | dict:
    name = "time_is_ordered"
    if not calls:
        return _skip(name, "no calls to check")
    backwards = [
        c.get("call_id") or c.get("start_time")
        for c in calls
        if (s := _seconds(c)) is not None and s < 0
    ]
    unparseable = [c.get("call_id") for c in calls if _seconds(c) is None]
    if backwards:
        return _fail(name, f"{len(backwards)} call(s) end before they start")
    if unparseable:
        return _fail(name, f"{len(unparseable)} call(s) have unreadable timestamps")
    return _ok(name, f"all {len(calls)} calls end at or after they start")


def _decoding_rate_is_possible(calls: list[dict]) -> dict:
    name = "decoding_rate_is_possible"
    worst, worst_rate = None, 0.0
    for c in calls:
        seconds = _seconds(c)
        out = int(c.get("output_tokens") or 0)
        if not seconds or seconds <= 0 or not out:
            continue
        rate = out / seconds
        if rate > worst_rate:
            worst, worst_rate = c, rate
    if worst is None:
        return _skip(name, "no call has both output tokens and a duration")
    if worst_rate > MAX_OUTPUT_TOKENS_PER_SECOND:
        return _fail(
            name,
            f"{worst_rate:,.0f} output tokens/second claimed "
            f"({worst.get('output_tokens'):,} tokens in {_seconds(worst):.3f}s); "
            f"nothing decodes faster than about {MAX_OUTPUT_TOKENS_PER_SECOND:,}",
        )
    return _ok(name, f"peak {worst_rate:,.0f} output tokens/second")


def _calls_fit_their_window(calls: list[dict], fields: dict) -> dict:
    name = "calls_fit_their_window"
    start, end = fields.get("agent_start_time"), fields.get("agent_end_time")
    if not calls or not start or not end:
        return _skip(name, "no measurement window to check against")
    try:
        window = (parse_ts(end) - parse_ts(start)).total_seconds()
    except (ValueError, TypeError):
        return _skip(name, "window timestamps unreadable")
    busy = sum(s for c in calls if (s := _seconds(c)) and s > 0)
    # Calls may legitimately overlap - a runtime can have several in flight - so
    # this only catches busy time far beyond what concurrency explains.
    if window > 0 and busy > window * 8:
        return _fail(
            name,
            f"calls claim {busy:,.0f}s of work inside a {window:,.0f}s window, "
            "more than plausible concurrency accounts for",
        )
    return _ok(name, f"{busy:,.0f}s of call time across a {window:,.0f}s window")


def _no_single_call_holds_a_session(calls: list[dict]) -> dict:
    name = "no_single_call_holds_a_session"
    for c in calls:
        total = int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0)
        if total > MAX_TOKENS_PER_CALL:
            return _fail(
                name,
                f"one call claims {total:,} tokens; that is a session reported as "
                "a single request, not a single request",
            )
    return _ok(name, f"largest call is within {MAX_TOKENS_PER_CALL:,} tokens")


def _totals_match_the_calls(calls: list[dict], fields: dict) -> dict:
    """The stored totals are derived from the calls, so this is an integrity
    check on the pipeline rather than on the submitter - it fires if a field
    was ever set from something other than the call records."""
    name = "totals_match_the_calls"
    if not calls:
        return _skip(name, "no calls to total")
    for key, source in (("input_tokens", "input_tokens"),
                        ("output_tokens", "output_tokens")):
        expected = sum(int(c.get(source) or 0) for c in calls)
        if fields.get(key) is not None and int(fields[key]) != expected:
            return _fail(
                name,
                f"{key} is {fields[key]:,} but the call records sum to {expected:,}",
            )
    if fields.get("total_api_request_count") not in (None, len(calls)):
        return _fail(
            name,
            f"total_api_request_count is {fields['total_api_request_count']} "
            f"but {len(calls)} call records were supplied",
        )
    return _ok(name, "totals equal the sum of the call records")
