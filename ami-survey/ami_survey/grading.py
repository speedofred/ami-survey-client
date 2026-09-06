"""Validation of `agent_output_grade` against the configured AMI grading scale."""

from __future__ import annotations

import json
from functools import lru_cache

from . import config


class GradingError(ValueError):
    """Raised when a submitted grade does not satisfy the configured scale."""


@lru_cache(maxsize=1)
def scale() -> dict:
    if not config.GRADING_SCALE_FILE.exists():
        raise FileNotFoundError(
            f"Grading scale not found at {config.GRADING_SCALE_FILE}."
        )
    return json.loads(config.GRADING_SCALE_FILE.read_text())


def grade_codes() -> list[str]:
    return [g["code"] for g in scale()["grades"]]


def validate(grade: str, justification: str | None, evidence: list | None, grader: str) -> dict:
    """Validate a grade submission and return the normalised grading record."""
    sc = scale()
    codes = grade_codes()
    if grade not in codes:
        raise GradingError(
            f"agent_output_grade must be one of {codes} (scale {sc['scale_id']}); got {grade!r}."
        )

    allowed_graders = sc.get("allowed_graders", ["self"])
    if grader not in allowed_graders:
        raise GradingError(f"grader must be one of {allowed_graders}; got {grader!r}.")

    justification = (justification or "").strip()
    evidence = [e for e in (evidence or []) if str(e).strip()]

    if grade != "NOT_GRADED":
        min_chars = int(sc.get("min_justification_chars", 0))
        if sc.get("requires_justification") and len(justification) < min_chars:
            raise GradingError(
                f"grade_justification must be at least {min_chars} characters "
                f"(got {len(justification)}). State what the output was and how it "
                "measured against the workflow's requirements."
            )
        if sc.get("requires_evidence") and not evidence:
            raise GradingError(
                "grade_evidence is required: list the concrete artifacts being graded "
                "(file paths, ticket ids, message ids, or tool-call outputs)."
            )

    entry = next(g for g in sc["grades"] if g["code"] == grade)
    return {
        "grade": grade,
        "grade_label": entry["label"],
        "grade_numeric_value": entry.get("numeric_value"),
        "scale_id": sc["scale_id"],
        "grader": grader,
        "justification": justification,
        "evidence": evidence,
    }
