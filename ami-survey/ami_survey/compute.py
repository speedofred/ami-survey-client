"""Turns raw call telemetry into the collection-inventory field values.

Everything here is arithmetic over recorded call records. If an input is missing,
the corresponding field comes out as null with a provenance note - it is never
back-filled with an estimate.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from . import inventory, phases, pricing, trust
from .timeutil import max_ts, min_ts, parse_ts

CONFIDENCE_RANK = {
    "observed_high": 3, "observed_medium": 2, "observed_low": 1,
    # Declared attribution, ranked against itself: a stage with a known end
    # outranks one that simply ran to the end of the measurement window.
    "declared_explicit": 3, "declared_open_ended": 2, "unavailable": 0,
}


def _sum(calls: list[dict], key: str) -> int:
    return sum(int(c.get(key) or 0) for c in calls)


def _sum_or_none(calls: list[dict], key: str) -> int | None:
    """A total, or None when nobody reported the quantity at all.

    Only some providers report reasoning tokens separately; Anthropic folds them
    into output_tokens and says nothing. Summing to 0 there would read as "this
    model did no thinking", which is a claim, and a false one. Absent says what
    is true: nothing measured it.
    """
    reported = [c[key] for c in calls if c.get(key) is not None]
    return sum(int(v) for v in reported) if reported else None


def _primary_model(calls: list[dict]) -> str | None:
    """The model that did most of the work, measured by total tokens."""
    if not calls:
        return None
    totals: Counter[str] = Counter()
    for c in calls:
        m = c.get("model")
        if m:
            totals[m] += int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0)
    if not totals:
        return None
    return totals.most_common(1)[0][0]


def annotate_calls(calls: list[dict], stage_markers: list[dict]) -> list[dict]:
    """Attach declared-stage and observed-phase attribution to each call."""
    out = []
    for c in calls:
        c = dict(c)
        phase, phase_conf = phases.classify_call(
            c.get("tool_calls"), bool(c.get("has_text")), bool(c.get("has_thinking"))
        )
        c["observed_execution_phase"] = phase
        c["observed_execution_phase_confidence"] = phase_conf
        declared = c.get("workflow_stage")
        if declared:
            c["workflow_stage_confidence"] = "declared_explicit"
        else:
            stage, conf = phases.stage_for_timestamp(stage_markers, c["start_time"])
            c["workflow_stage"] = stage
            c["workflow_stage_confidence"] = conf
        out.append(c)
    return out


def _aggregate(group: list[dict]) -> dict:
    start = min_ts([c["start_time"] for c in group])
    end = max_ts([c["end_time"] for c in group])
    elapsed = round((parse_ts(end) - parse_ts(start)).total_seconds(), 3)
    inp, outp = _sum(group, "input_tokens"), _sum(group, "output_tokens")
    return {
        "stage_agent_call_count": len(group),
        "stage_input_tokens": inp,
        "stage_output_tokens": outp,
        "stage_reasoning_tokens": _sum_or_none(group, "reasoning_output_tokens"),
        "stage_total_tokens": inp + outp,
        "stage_start_time": start,
        "stage_end_time": end,
        "stage_agent_runtime": elapsed,
    }


def _timed_profile(markers: list[dict], ends_at: str | None = None) -> list[dict]:
    """Stage rows built from markers alone, for a run with no call records.

    A browser agent cannot report tokens or call counts, but if it marked its
    stages while working, the server watched those boundaries arrive. That is
    real elapsed time per stage and it belongs in the profile - with the columns
    nobody measured left empty rather than filled with a zero, which would read
    as "this stage cost nothing".
    """
    # The final stage has no marker after it, so without a closing boundary it
    # would simply vanish from the profile - the run that prompted this reported
    # two stages and got one row. `ends_at` is that boundary: an observed closing
    # marker if there was one, else the moment the survey opened, which the work
    # necessarily finished before.
    boundaries = list(markers)
    if ends_at and not (boundaries and boundaries[-1].get("closes")):
        boundaries.append({"stage": "(end of work)", "closes": True,
                           "marked_at": ends_at})
    rows = []
    for marker, nxt in zip(boundaries, boundaries[1:]):
        if marker.get("closes"):
            break
        rows.append({
            "phase_basis": "Declared Workflow Stage",
            "workflow_stage": marker["stage"],
            "workflow_stage_confidence": "declared_explicit",
            "observed_execution_phase": None,
            "observed_execution_phase_confidence": None,
            "stage_agent_call_count": None,
            "stage_input_tokens": None,
            "stage_output_tokens": None,
            "stage_reasoning_tokens": None,
            "stage_total_tokens": None,
            "stage_start_time": marker["marked_at"],
            "stage_end_time": nxt["marked_at"],
            "stage_agent_runtime": round(
                (parse_ts(nxt["marked_at"]) - parse_ts(marker["marked_at"])
                 ).total_seconds(), 3),
        })
    return rows


def effort_profile(calls: list[dict]) -> list[dict]:
    """One row per declared workflow stage, plus one per observed phase for the
    calls no declared stage covers. Matches the Agent Effort Profile fields."""
    declared: dict[str, list[dict]] = defaultdict(list)
    observed: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        if c.get("workflow_stage"):
            declared[c["workflow_stage"]].append(c)
        else:
            observed[c["observed_execution_phase"]].append(c)

    rows: list[dict] = []
    for stage, group in declared.items():
        # The row carries the weakest confidence of the calls in it, so a stage
        # nothing closes cannot be read as though it had a known end.
        confidence = min(
            (c.get("workflow_stage_confidence") or "declared_explicit" for c in group),
            key=lambda c: CONFIDENCE_RANK.get(c, 0),
        )
        rows.append(
            {
                "phase_basis": "Declared Workflow Stage",
                "workflow_stage": stage,
                "workflow_stage_confidence": confidence,
                "observed_execution_phase": None,
                "observed_execution_phase_confidence": "not_applicable",
                **_aggregate(group),
                "_observed_phase_mix": dict(
                    Counter(c["observed_execution_phase"] for c in group)
                ),
            }
        )
    for phase, group in observed.items():
        confidences = [c["observed_execution_phase_confidence"] for c in group]
        weakest = min(confidences, key=lambda c: CONFIDENCE_RANK.get(c, 0))
        rows.append(
            {
                "phase_basis": "Observed Execution Phase",
                "workflow_stage": None,
                "workflow_stage_confidence": "unavailable",
                "observed_execution_phase": phase,
                "observed_execution_phase_confidence": weakest,
                **_aggregate(group),
            }
        )
    rows.sort(key=lambda r: r["stage_start_time"])
    return rows


def derived_analysis(fields: dict, calls: list[dict], price: pricing.ModelPricing) -> dict:
    """Figures useful to a human reading the results.

    These are NOT collection-inventory fields - the survey collects exactly the
    inventory. They are reported separately, and clearly labelled as derived.
    """
    breakdown = Counter()
    for c in calls:
        for k, v in (c.get("input_token_breakdown") or {}).items():
            breakdown[k] += int(v or 0)

    inp = fields.get("input_tokens") or 0
    out = fields.get("output_tokens") or 0
    ip, op = fields.get("input_price_per_1m"), fields.get("output_price_per_1m")

    flat_cost = None
    if ip is not None and op is not None:
        flat_cost = round(inp / 1e6 * ip + out / 1e6 * op, 6)

    cache_aware_cost = None
    if ip is not None and op is not None:
        cw = price.cache_write_price_per_1m if price.cache_write_price_per_1m is not None else ip
        cr = price.cache_read_price_per_1m if price.cache_read_price_per_1m is not None else ip
        cache_aware_cost = round(
            breakdown["uncached_input_tokens"] / 1e6 * ip
            + breakdown["cache_creation_input_tokens"] / 1e6 * cw
            + breakdown["cache_read_input_tokens"] / 1e6 * cr
            + out / 1e6 * op,
            6,
        )

    runtime = fields.get("total_agent_runtime") or 0
    wf_seconds = None
    if fields.get("workflow_start_time") and fields.get("workflow_end_time"):
        wf_seconds = round(
            (
                parse_ts(fields["workflow_end_time"]) - parse_ts(fields["workflow_start_time"])
            ).total_seconds(),
            3,
        )

    return {
        "_note": "Derived for human analysis; not part of Collection_Inventory.csv.",
        "input_token_breakdown": dict(breakdown),
        "total_tokens": inp + out,
        "estimated_cost_usd_flat_rate": flat_cost,
        "estimated_cost_usd_cache_aware": cache_aware_cost,
        "cost_basis": (
            "flat = tokens x list price; cache_aware also applies the model's cache "
            "write/read rates to the cached portion of input."
        ),
        "workflow_elapsed_seconds": wf_seconds,
        "agent_share_of_workflow": (
            round(runtime / wf_seconds, 4) if wf_seconds else None
        ),
        "mean_seconds_per_call": (
            round(runtime / fields["total_api_request_count"], 3)
            if fields.get("total_api_request_count")
            else None
        ),
        "output_tokens_per_agent_second": (round(out / runtime, 2) if runtime else None),
        "models_observed": dict(Counter(c.get("model") for c in calls if c.get("model"))),
        "sidechain_call_count": sum(1 for c in calls if c.get("is_sidechain")),
    }


def build_response(run: dict) -> dict:
    """Assemble the complete survey response for a run."""
    calls = annotate_calls(run.get("calls", []), run.get("stage_markers", []))
    answers = run.get("answers", {})
    meta = run.get("runtime_metadata", {}) or {}
    grading = run.get("grading") or {}

    model = _primary_model(calls) or meta.get("model_name")
    price = pricing.resolve(
        model, meta.get("pricing_provider_hint")
    ) if model else pricing.ModelPricing(
        model_name="", matched_key=None, provider=None, input_price_per_1m=None,
        output_price_per_1m=None, cache_write_price_per_1m=None,
        cache_read_price_per_1m=None, source="no model observed", resolved=False,
    )

    fields: dict = {
        "input_tokens": _sum(calls, "input_tokens") if calls else None,
        "output_tokens": _sum(calls, "output_tokens") if calls else None,
        # A component of output_tokens, not a peer: adding it to a total would
        # count the same tokens twice.
        "reasoning_tokens": _sum_or_none(calls, "reasoning_output_tokens"),
        "total_api_request_count": len(calls) if calls else None,
        "agent_start_time": (min_ts([c["start_time"] for c in calls]) if calls
                             else _observed_bound(run, "workflow_start_time")),
        "agent_end_time": (max_ts([c["end_time"] for c in calls]) if calls
                           else _observed_bound(run, "workflow_end_time")),
        "total_agent_runtime": (
            round(sum(float(c.get("duration_seconds") or 0) for c in calls), 3)
            if calls
            else _observed_span(run)
        ),
        "input_price_per_1m": price.input_price_per_1m,
        "output_price_per_1m": price.output_price_per_1m,
        "workflow_start_time": run.get("workflow_start_time"),
        "workflow_end_time": run.get("workflow_end_time"),
        "agent_output_grade": grading.get("grade"),
        "workflow_name": answers.get("workflow_name"),
        "workflow_description": answers.get("workflow_description"),
        # Declared once by the workflow, carried on every run of it. Null where
        # the submitter declared nothing, which is a fact about the run rather
        # than a gap in the measurement.
        "workflow_category": answers.get("workflow_category"),
        "work_unit": answers.get("work_unit"),
        "work_unit_count": answers.get("work_unit_count"),
        "model_name": model,
        "model_provider": price.provider or meta.get("model_provider"),
        "platform": meta.get("platform"),
    }

    profile = effort_profile(calls)
    if not profile:
        # No calls to group, but possibly stages the server watched happen.
        profile = _timed_profile(
            [m for m in run.get("stage_markers") or []
             if m.get("marked_at_source") == "observed when the marker arrived"],
            ends_at=_observed_bound(run, "workflow_end_time"),
        )

    provenance = {
        name: _provenance_for(name, run, price, calls, meta)
        for name in inventory.BY_NAME
    }

    adapter = run.get("telemetry_adapter")
    return {
        "fields": fields,
        "agent_effort_profile": profile,
        # Where these numbers came from, and whether they could be true. First
        # class, not a footnote: the difference between a number an adapter read
        # and one an agent typed is the whole premise of the benchmark.
        "trust": trust.tier_block(
            adapter, calls,
            # Absent on runs opened before this was recorded. Defaulting to True
            # is the wrong answer for any of those that came in on a self-issued
            # token - one already had - so stored responses were backfilled from
            # the owner id, which is the token id and therefore a lookup rather
            # than a guess. The default only covers runs opened with no auth at
            # all, which is a developer's own machine.
            corroborated=run.get("token_corroborated", True),
        ),
        # Who says they ran this. Recorded from the token at registration, so it
        # is stable across a submitter's runs, and it is a claim either way.
        "agent_identity": run.get("agent_identity"),
        "plausibility": trust.check(calls, fields, adapter),
        "provenance": provenance,
        "grading": grading,
        "derived_analysis": derived_analysis(fields, calls, price),
        "pricing_resolution": price.to_dict(),
        "runtime_metadata": meta,
        "calls": calls,
    }


def _observed_bound(run: dict, key: str) -> str | None:
    """A workflow boundary, but only when the server watched it rather than being
    told. An agent-supplied time is a claim about the workflow, not a record of
    when the agent was running."""
    source = (run.get("workflow_time_source") or {}).get(key) or ""
    return run.get(key) if source.startswith("observed") else None


def _observed_span(run: dict) -> float | None:
    start = _observed_bound(run, "workflow_start_time")
    end = _observed_bound(run, "workflow_end_time")
    if not (start and end):
        return None
    return round((parse_ts(end) - parse_ts(start)).total_seconds(), 3)


def _provenance_for(
    name: str, run: dict, price: pricing.ModelPricing, calls: list[dict], meta: dict
) -> dict:
    field = inventory.BY_NAME[name]
    acq = field.acquisition
    adapter = run.get("telemetry_adapter")

    # The inventory says how a field is *meant* to be acquired. What actually
    # happened depends on the adapter: a run whose numbers arrived over the API
    # did not acquire them from runtime telemetry, whatever the field says, and
    # recording otherwise would let a claim pass as a measurement to anyone
    # filtering on this key.
    if acq == "runtime_telemetry" and trust.tier_for(adapter, calls) != trust.MEASURED:
        acq = "self_reported"

    base = {"acquisition": acq, "scope": field.scope}

    if acq in ("runtime_telemetry", "self_reported"):
        base |= {
            "source": adapter or "unknown",
            "evidence": (
                f"{len(calls)} deduplicated API call records"
                if acq == "runtime_telemetry"
                else f"{len(calls)} call records supplied by the submitting agent"
            ),
            "method": {
                "input_tokens": "sum of uncached + cache-creation + cache-read input "
                                "tokens reported by the provider, per request id",
                "output_tokens": "sum of provider-reported output tokens per request id",
                "total_api_request_count": "count of distinct provider request ids",
                "agent_start_time": "earliest call start in the measurement window",
                "agent_end_time": "latest call completion in the measurement window",
                "total_agent_runtime": "sum of per-call elapsed seconds "
                                       "(request trigger to response completion)",
            }.get(name),
        }
    elif acq == "pricing_lookup":
        base |= {
            "source": price.source,
            "matched_model_key": price.matched_key,
            "resolved": price.resolved,
            "evidence": "LiteLLM model price map lookup for the observed model id",
        }
    elif acq == "runtime_metadata":
        base |= {
            "source": adapter or "unknown",
            "evidence": {
                "model_name": "model id recorded on the observed API responses",
                "model_provider": f"litellm_provider for {price.matched_key!r}",
                "platform": f"runtime metadata: {meta.get('runtime')} "
                            f"{meta.get('entrypoint')} v{meta.get('version')}",
            }.get(name),
        }
    elif acq == "workflow_marker":
        base |= {
            "source": run.get("workflow_time_source", {}).get(name, "unset"),
            "evidence": "explicit marker recorded by the survey API clock",
        }
    elif acq == "agent_reported":
        base |= {
            "source": "agent",
            "evidence": "supplied by the agent when starting the survey; describes the "
                        "workflow it was tasked with, not any measured quantity",
        }
    elif acq == "graded":
        g = run.get("grading") or {}
        base |= {
            "source": f"grader={g.get('grader', 'unset')}",
            "scale_id": g.get("scale_id"),
            "evidence": g.get("evidence"),
            "justification": g.get("justification"),
        }
    elif acq in ("derived_aggregate", "phase_attribution"):
        base |= {
            "source": "computed by the survey API from the call records",
            "evidence": "see agent_effort_profile rows and calls[]",
        }
    return base
