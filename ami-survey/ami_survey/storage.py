"""Disk persistence for survey runs and submitted responses.

Everything is plain JSON, plus a flat CSV index whose columns are exactly the
collection inventory - so the results can be opened in a spreadsheet without any
tooling from this project.
"""

from __future__ import annotations

import csv
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config, inventory
from .timeutil import utcnow  # re-exported: storage.utcnow is the API's clock

_lock = threading.Lock()


def slug(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "workflow"


def _run_path(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return config.RUNS_DIR / f"{run_id}.json"


def save_run(run: dict) -> Path:
    config.ensure_dirs()
    path = _run_path(run["run_id"])
    run["updated_at"] = utcnow()
    with _lock:
        path.write_text(json.dumps(run, indent=2, ensure_ascii=False))
    return path


def load_run(run_id: str) -> dict | None:
    path = _run_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_runs() -> list[dict]:
    config.ensure_dirs()
    out = []
    for p in sorted(config.RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            r = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "run_id": r.get("run_id"),
                "status": r.get("status"),
                # The token that opened the run, so the API can show a caller
                # only their own runs when it is enforcing auth.
                "owner": r.get("owner"),
                "workflow_name": (r.get("answers") or {}).get("workflow_name"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "call_count": len(r.get("calls") or []),
                "response_path": r.get("response_path"),
            }
        )
    return out


def save_response(run_id: str, response: dict) -> dict:
    """Persist a submitted survey response: JSON + CSV index row."""
    config.ensure_dirs()
    name = (response.get("fields") or {}).get("workflow_name") or "workflow"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}_{slug(name)}_{run_id}"
    json_path = config.RESPONSES_DIR / f"{base}.json"

    with _lock:
        json_path.write_text(json.dumps(response, indent=2, ensure_ascii=False))
        _append_index(response)

    return {"json": str(json_path), "index": str(index_path())}


def delete_response(run_id: str) -> dict:
    """Remove a submitted response completely, index and run record included.

    Deleting the two response files by hand is the obvious move and it leaves
    the dataset inconsistent: the CSV index keeps its rows and the in-flight run
    record is orphaned, so `ami-report` and the dashboard disagree about what
    exists. Done here as one operation so there is nothing to remember.

    The index is *rebuilt* from the responses that remain rather than filtered.
    Rebuilding is self-healing - it repairs drift from any cause, including a
    hand-deletion that happened before this existed.
    """
    config.ensure_dirs()
    removed = []
    with _lock:
        for path in sorted(config.RESPONSES_DIR.glob(f"*_{run_id}.*")):
            if path.suffix in (".json", ".md"):
                path.unlink()
                removed.append(str(path))
        run_path = config.RUNS_DIR / f"{run_id}.json"
        if run_path.exists():
            run_path.unlink()
            removed.append(str(run_path))
        rows_before, rows_after = rebuild_index()
    return {
        "run_id": run_id,
        "removed": removed,
        "index_rows_before": rows_before,
        "index_rows_after": rows_after,
    }


def rebuild_index() -> tuple[int, int]:
    """Regenerate index.csv from the responses on disk. Returns (before, after).

    Every row is derived from a response file, so anything the index claims that
    no longer exists is dropped, and anything on disk that never made it in is
    added back.
    """
    path = index_path()
    before = 0
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            before = sum(1 for _ in csv.DictReader(fh))
        path.unlink()

    after = 0
    for response_path in sorted(config.RESPONSES_DIR.glob("*.json")):
        try:
            response = json.loads(response_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not response.get("run_id"):
            continue
        _append_index(response)
        after += len(response.get("agent_effort_profile") or [{}])
    return before, after


def index_path() -> Path:
    return config.RESPONSES_DIR / "index.csv"


#: characters that make a spreadsheet treat a cell as a formula rather than text
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise spreadsheet formula injection in a cell.

    `workflow_name` and `workflow_description` are free text supplied by whoever
    took the survey. A value beginning `=` or `@` is executed as a formula when
    the index is opened in Excel, Sheets or Numbers, which is a code-execution
    path from a survey submission to the analyst's laptop. Quoting alone does not
    stop it - the leading character has to be defused.
    """
    if not isinstance(value, str) or not value.startswith(_FORMULA_LEAD):
        return value
    return "'" + value


def _append_index(response: dict) -> None:
    """One CSV row per Agent Effort Profile row, carrying the run-level fields.

    Columns are the inventory field order, prefixed with run identifiers, so the
    file is a direct tabular rendering of Collection_Inventory.csv.
    """
    path = index_path()
    columns = ["run_id", "submitted_at"] + [f.name for f in inventory.FIELDS]
    new = not path.exists()
    run_values = {f.name: response["fields"].get(f.name) for f in inventory.RUN_FIELDS}
    rows = response.get("agent_effort_profile") or [{}]

    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        for stage_row in rows:
            row = {
                "run_id": response["run_id"],
                "submitted_at": response["submitted_at"],
                **run_values,
            }
            for f in inventory.STAGE_FIELDS:
                row[f.name] = stage_row.get(f.name)
            writer.writerow({k: csv_safe(v) for k, v in row.items()})


def list_responses() -> list[dict]:
    config.ensure_dirs()
    out = []
    for p in sorted(config.RESPONSES_DIR.glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        f = r.get("fields", {})
        out.append(
            {
                "path": str(p),
                "run_id": r.get("run_id"),
                "submitted_at": r.get("submitted_at"),
                "workflow_name": f.get("workflow_name"),
                "model_name": f.get("model_name"),
                "total_api_request_count": f.get("total_api_request_count"),
                "input_tokens": f.get("input_tokens"),
                "output_tokens": f.get("output_tokens"),
                "agent_output_grade": f.get("agent_output_grade"),
                # For the spread tables: how long the agent ran, and the grade as
                # a number so a range across repeats means something.
                "total_agent_runtime": f.get("total_agent_runtime"),
                "grade_numeric_value": (r.get("grading") or {}).get("grade_numeric_value"),
                "estimated_cost_usd": (r.get("derived_analysis") or {}).get(
                    "estimated_cost_usd_cache_aware"
                ),
                # A count, not the text: enough to sort and flag by, without
                # making the index as big as the responses it indexes.
                "warnings": len(r.get("warnings") or []),
                # Derived when the response was built, so an older response
                # without the field is read as what it was: adapter-measured.
                "trust_tier": (r.get("trust") or {}).get("tier", "measured"),
                # Whether anyone vetted the submitter. Absent on responses that
                # predate the field, which all came from hand-issued tokens.
                "corroborated": (r.get("trust") or {}).get("corroborated", True),
                "agent_name": ((r.get("agent_identity") or {}) or {}).get("name"),
                "plausibility_failed": len((r.get("plausibility") or {}).get("failed") or []),
            }
        )
    return out


def load_response(run_id: str) -> dict | None:
    for p in sorted(config.RESPONSES_DIR.glob(f"*_{run_id}.json"), reverse=True):
        return json.loads(p.read_text())
    return None
