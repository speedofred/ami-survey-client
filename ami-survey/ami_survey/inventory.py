"""The survey definition, derived strictly from Collection_Inventory.csv.

The CSV is the contract: the survey collects exactly those fields, no more and
no less. This module parses it and attaches, for every field, *how* the value is
allowed to be obtained. Any drift between the CSV and the acquisition table
raises at import time rather than silently producing a partial survey.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from typing import Literal

from . import config

# How a field's value may legitimately be produced. No field may be answered by
# free-form model recall; "agent_reported" is the only prose input and it is
# limited to naming/describing the workflow the agent was asked to survey.
Acquisition = Literal[
    "runtime_telemetry",  # measured from the runtime's own call records
    "pricing_lookup",  # resolved from the LiteLLM price map for the observed model
    "runtime_metadata",  # read from the runtime's own environment/transcript metadata
    "workflow_marker",  # timestamp recorded by an explicit marker call
    "agent_reported",  # supplied by the agent: workflow name / description
    "workflow_declared",  # read from the workflow's own definition, not the agent's account of it
    "graded",  # quality grade against the configured AMI grading scale
    "derived_aggregate",  # computed by the API from telemetry (stage roll-ups)
    "phase_attribution",  # declared stage marker, or observed-behaviour classifier
]

# Scope of a field: one value per run, or one value per Agent Effort Profile row.
Scope = Literal["run", "stage"]

ACQUISITION: dict[str, tuple[Acquisition, Scope]] = {
    "input_tokens": ("runtime_telemetry", "run"),
    "output_tokens": ("runtime_telemetry", "run"),
    "reasoning_tokens": ("runtime_telemetry", "run"),
    "total_api_request_count": ("runtime_telemetry", "run"),
    "agent_start_time": ("runtime_telemetry", "run"),
    "agent_end_time": ("runtime_telemetry", "run"),
    "total_agent_runtime": ("runtime_telemetry", "run"),
    "input_price_per_1m": ("pricing_lookup", "run"),
    "output_price_per_1m": ("pricing_lookup", "run"),
    "workflow_start_time": ("workflow_marker", "run"),
    "workflow_end_time": ("workflow_marker", "run"),
    "agent_output_grade": ("graded", "run"),
    "workflow_name": ("agent_reported", "run"),
    "workflow_description": ("agent_reported", "run"),
    "workflow_category": ("workflow_declared", "run"),
    "work_unit": ("workflow_declared", "run"),
    "work_unit_count": ("workflow_declared", "run"),
    "model_name": ("runtime_metadata", "run"),
    "model_provider": ("runtime_metadata", "run"),
    "platform": ("runtime_metadata", "run"),
    "workflow_stage": ("phase_attribution", "stage"),
    "workflow_stage_confidence": ("phase_attribution", "stage"),
    "observed_execution_phase": ("phase_attribution", "stage"),
    "observed_execution_phase_confidence": ("phase_attribution", "stage"),
    "phase_basis": ("phase_attribution", "stage"),
    "stage_agent_call_count": ("derived_aggregate", "stage"),
    "stage_input_tokens": ("derived_aggregate", "stage"),
    "stage_output_tokens": ("derived_aggregate", "stage"),
    "stage_reasoning_tokens": ("derived_aggregate", "stage"),
    "stage_total_tokens": ("derived_aggregate", "stage"),
    "stage_start_time": ("derived_aggregate", "stage"),
    "stage_end_time": ("derived_aggregate", "stage"),
    "stage_agent_runtime": ("derived_aggregate", "stage"),
}

# Human-facing prompt shown for the two fields the agent actually answers in
# prose, plus the graded field. Everything else is presented as "auto-collected".
QUESTION_TEXT: dict[str, str] = {
    "workflow_name": (
        "What is the name of the workflow you just ran? Use a short, stable, "
        "reusable label (e.g. 'Support Ticket Triage & Response') - not a "
        "description of this one instance."
    ),
    "workflow_description": (
        "Describe the workflow and the business work it performs, in 1-3 "
        "sentences. State what was given to you and what you produced."
    ),
    "agent_output_grade": (
        "Grade the output you produced for this workflow against the AMI "
        "grading scale (call ami_get_grading_scale for the rubric). You must "
        "cite the concrete artifacts you are grading."
    ),
}


@dataclass(frozen=True)
class Field:
    name: str
    description: str
    data_type: str
    categories: list[str]
    acquisition: Acquisition
    scope: Scope
    question: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _load() -> list[Field]:
    if not config.INVENTORY_CSV.exists():
        raise FileNotFoundError(
            f"Collection inventory not found at {config.INVENTORY_CSV}. "
            "Set AMI_INVENTORY_CSV to its location."
        )
    fields: list[Field] = []
    with config.INVENTORY_CSV.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("field") or "").strip()
            if not name:
                continue  # trailing blank line
            if name not in ACQUISITION:
                raise ValueError(
                    f"Inventory field {name!r} has no acquisition rule. Every field in "
                    "Collection_Inventory.csv must be concretely obtainable; add a rule "
                    "in ami_survey/inventory.py::ACQUISITION."
                )
            acquisition, scope = ACQUISITION[name]
            fields.append(
                Field(
                    name=name,
                    description=(row.get("description") or "").strip(),
                    data_type=(row.get("data_type") or "").strip(),
                    categories=[
                        c.strip() for c in (row.get("category") or "").split(";") if c.strip()
                    ],
                    acquisition=acquisition,
                    scope=scope,
                    question=QUESTION_TEXT.get(name),
                )
            )
    known = {f.name for f in fields}
    missing = set(ACQUISITION) - known
    if missing:
        raise ValueError(
            f"Acquisition rules exist for fields absent from the inventory: {sorted(missing)}. "
            "The survey must match the inventory exactly."
        )
    return fields


FIELDS: list[Field] = _load()
BY_NAME: dict[str, Field] = {f.name: f for f in FIELDS}
RUN_FIELDS: list[Field] = [f for f in FIELDS if f.scope == "run"]
STAGE_FIELDS: list[Field] = [f for f in FIELDS if f.scope == "stage"]


def agent_answered_fields() -> list[Field]:
    """Fields the agent must actually answer; everything else is measured."""
    return [f for f in FIELDS if f.acquisition in ("agent_reported", "graded")]


def workflow_declared_fields() -> list[Field]:
    """Fields read from the workflow's own definition file.

    Neither measured nor recalled. A workflow states its category and its unit
    of work once, in `workflow.json`, and every run of it carries the same
    answer - which is the point: a category that changed per run could not
    group anything.
    """
    return [f for f in FIELDS if f.acquisition == "workflow_declared"]


def survey_document() -> dict:
    """The survey as served by the API."""
    return {
        "survey_id": "ami-collection-inventory",
        # The file this was built from, by name. The full path is where it
        # lives on one particular machine, which is nobody else's business
        # and has leaked from three other endpoints already.
        "source_inventory": config.INVENTORY_CSV.name,
        "field_count": len(FIELDS),
        "principle": (
            "Every field is obtained from runtime evidence, a pricing lookup, or an "
            "explicit graded judgement. No field may be estimated from recall."
        ),
        "sections": {
            "auto_collected": [
                f.to_dict()
                for f in FIELDS
                if f.acquisition
                not in ("agent_reported", "graded", "workflow_declared")
                and f.scope == "run"
            ],
            # Its own section rather than folded into auto_collected: these are
            # not measured from anything, they are what the workflow says about
            # itself, and a submitter needs to know they come from workflow.json
            # rather than being watched for.
            "workflow_declared": [f.to_dict() for f in workflow_declared_fields()],
            "agent_answered": [f.to_dict() for f in agent_answered_fields()],
            "agent_effort_profile": [f.to_dict() for f in STAGE_FIELDS],
        },
        "fields": [f.to_dict() for f in FIELDS],
    }
