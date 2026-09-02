"""Validation of a workflow's declared category and unit of work.

Two things a workflow says about itself that nothing can measure for it, and
that scoring cannot do without:

**The category** decides which other workflows it may be compared against. It is
declared, never inferred. Inferring it from the workflow name - which is what the
Codex-side benchmark dashboard did - mis-files runs silently, and a mis-filed run
is ranked against work it never performed.

**The unit of work** gives cost and duration a denominator. `$1.60` is not a
score; `$0.27 per ticket` is. Without it, a workflow that triages six tickets is
compared against one that writes a single CV and loses on price for doing more
work. A workflow may decline to declare one - some genuinely have no countable
unit - and the run is then marked unnormalised: still comparable against other
runs of the same workflow, never admitted to a category cohort.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from . import config


class CategoryError(ValueError):
    """Raised when a declared category or work unit does not satisfy the vocabulary."""


#: A unit is a short noun a human would recognise on an axis label: "ticket",
#: "CV", "support email". Not a sentence, and not a count.
UNIT_PATTERN = re.compile(r"\A[A-Za-z][A-Za-z0-9 /-]{0,29}\Z")

#: Above this, someone has put a token count or a byte count in the unit count
#: field. A work unit is the thing a person would say they did N of.
MAX_WORK_UNITS = 100_000

#: Declared, but deliberately outside every cohort. Kept as a named constant
#: because both the vocabulary file and the cohort rules have to agree on it.
UNCOHORTED_CATEGORY = "other"


@lru_cache(maxsize=1)
def vocabulary() -> dict:
    if not config.WORKFLOW_CATEGORIES_FILE.exists():
        raise FileNotFoundError(
            f"Workflow category vocabulary not found at {config.WORKFLOW_CATEGORIES_FILE}."
        )
    return json.loads(config.WORKFLOW_CATEGORIES_FILE.read_text())


def category_ids() -> list[str]:
    return [c["id"] for c in vocabulary()["categories"]]


def category(category_id: str) -> dict | None:
    return next((c for c in vocabulary()["categories"] if c["id"] == category_id), None)


def forms_cohorts(category_id: str | None) -> bool:
    """Whether runs in this category may be compared against each other.

    `other` is the escape hatch for work that fits nothing in the vocabulary,
    which is precisely the work that has no peers. An undeclared category has
    none either.
    """
    return bool(category_id) and category_id != UNCOHORTED_CATEGORY


def validate(
    workflow_category: str | None,
    work_unit: str | None = None,
    work_unit_count: object = None,
) -> dict:
    """Validate a workflow's self-declaration and return the normalised record.

    All three are optional - a workflow that declares none of them is still
    surveyed, scored on everything that does not need them, and reported. What
    it loses is entry to a category cohort, which is stated on the record rather
    than left for a reader to work out.
    """
    declared = (workflow_category or "").strip() or None
    if declared is not None:
        ids = category_ids()
        if declared not in ids:
            raise CategoryError(
                f"workflow_category must be one of {ids} "
                f"(vocabulary {vocabulary()['vocabulary_id']}); got {declared!r}. "
                "Categories are declared, not invented: pick the one covering the "
                "work the grade is mostly about."
            )

    unit = (work_unit or "").strip() or None
    count = _work_unit_count(work_unit_count)

    # Half a declaration is worse than none: a unit with no count cannot divide,
    # and a count with no unit cannot be labelled or compared across workflows.
    if (unit is None) != (count is None):
        raise CategoryError(
            "work_unit and work_unit_count must be declared together. "
            f"Got work_unit={work_unit!r}, work_unit_count={work_unit_count!r}. "
            "State the thing the workflow produces or processes and how many of "
            'them this run handled, e.g. "ticket" and 6.'
        )

    if unit is not None and not UNIT_PATTERN.match(unit):
        raise CategoryError(
            f"work_unit must be a short noun naming what the workflow handles, "
            f'e.g. "ticket", "CV", "support email"; got {unit!r}.'
        )

    entry = category(declared) if declared else None
    return {
        "workflow_category": declared,
        "category_label": entry["label"] if entry else None,
        "vocabulary_id": vocabulary()["vocabulary_id"] if declared else None,
        "work_unit": unit,
        "work_unit_count": count,
        # The one derived fact worth storing rather than recomputing: whether
        # cost and duration can be expressed per unit at all. Everything
        # downstream keys off it, and it is not obvious from the fields alone.
        "normalised": count is not None,
        "cohort_eligible": forms_cohorts(declared) and count is not None,
    }


def _work_unit_count(raw: object) -> int | None:
    """A positive whole number of things, or nothing at all."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        # Accept the string form: it arrives from JSON, a CSV and a CLI flag.
        count = int(str(raw).strip())
    except (TypeError, ValueError):
        raise CategoryError(
            f"work_unit_count must be a whole number of work units; got {raw!r}."
        ) from None
    if count < 1:
        raise CategoryError(
            f"work_unit_count must be at least 1; got {count}. A run that handled "
            "no work units has nothing to divide cost by - leave both undeclared."
        )
    if count > MAX_WORK_UNITS:
        raise CategoryError(
            f"work_unit_count is {count:,}, above the {MAX_WORK_UNITS:,} ceiling. "
            "A work unit is the thing a person would say they did N of - tickets, "
            "CVs, documents - not tokens, characters or API calls."
        )
    return count
