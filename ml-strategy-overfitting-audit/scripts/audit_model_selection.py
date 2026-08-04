#!/usr/bin/env python3
"""Audit ML trial denominators, model-family stability, and post-selection.

Input CSV columns:
trial_id,candidate_id,family,status,training_metric,development_metric,
evaluation_metric,selected,manual_override,event_order,final_n,
selection_evidence_role

The script uses only the Python standard library.  It reports structural
selection violations but deliberately does not invent performance thresholds.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


TRIAL_FIELDS = (
    "trial_id",
    "candidate_id",
    "family",
    "status",
    "training_metric",
    "development_metric",
    "evaluation_metric",
    "selected",
    "manual_override",
    "event_order",
    "final_n",
    "selection_evidence_role",
)
SUCCESS_STATES = {"success", "succeeded", "completed"}
FAILURE_STATES = {"failed", "failure", "error"}


def _float_or_none(value: Any, field: str = "value", location: str = "record") -> float | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}: {field} must be numeric or empty") from exc
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _boolean(value: Any, field: str, row_id: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{row_id}: {field} must be true or false")


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary(values: Iterable[float]) -> dict[str, float | int | None]:
    finite = [value for value in values if math.isfinite(value)]
    q25 = _quantile(finite, 0.25)
    q75 = _quantile(finite, 0.75)
    return {
        "count": len(finite),
        "mean": sum(finite) / len(finite) if finite else None,
        "median": _quantile(finite, 0.5),
        "iqr": None if q25 is None or q75 is None else q75 - q25,
        "worst_q90": _quantile(finite, 0.9),
    }


def read_trials(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header")
        missing = [field for field in TRIAL_FIELDS if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
        rows: list[dict[str, Any]] = []
        for number, row in enumerate(reader, start=2):
            trial_id = str(row["trial_id"]).strip()
            if not trial_id:
                raise ValueError(f"{path}:{number}: trial_id must be non-empty")
            row_id = f"trial:{trial_id}"
            rows.append(
                {
                    "trial_id": trial_id,
                    "candidate_id": str(row["candidate_id"]).strip(),
                    "family": str(row["family"]).strip(),
                    "status": str(row["status"]).strip().lower(),
                    "training_metric": _float_or_none(row["training_metric"], "training_metric", f"{path}:{number}"),
                    "development_metric": _float_or_none(row["development_metric"], "development_metric", f"{path}:{number}"),
                    "evaluation_metric": _float_or_none(row["evaluation_metric"], "evaluation_metric", f"{path}:{number}"),
                    "selected": _boolean(row["selected"], "selected", row_id),
                    "manual_override": _boolean(row["manual_override"], "manual_override", row_id),
                    "event_order": _int_or_none(row["event_order"]),
                    "final_n": _int_or_none(row["final_n"]),
                    "selection_evidence_role": str(row["selection_evidence_role"]).strip().lower(),
                }
            )
        return rows


def _frequencies(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, dict[str, float | int | None]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[field]) or "<missing>"].append(row)
    result: dict[str, dict[str, float | int | None]] = {}
    for name, group in sorted(groups.items()):
        successful = [row for row in group if row["status"] in SUCCESS_STATES]
        selected = [row for row in successful if row["selected"]]
        result[name] = {
            "attempted": len(group),
            "successful": len(successful),
            "selected": len(selected),
            "selection_frequency": len(selected) / len(successful) if successful else None,
        }
    return result


def audit_model_selection(rows: Sequence[Mapping[str, Any]], metric_direction: str = "higher") -> dict[str, Any]:
    """Summarize trial evidence and flag selections not based on development data."""

    if metric_direction not in {"higher", "lower"}:
        raise ValueError("metric_direction must be higher or lower")
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    train_development_gaps: list[float] = []
    development_evaluation_gaps: list[float] = []
    manual_overrides: list[dict[str, Any]] = []

    for row in rows:
        trial_id = str(row["trial_id"])
        if trial_id in seen:
            issues.append({"code": "DUPLICATE_TRIAL_ID", "severity": "ERROR", "trial_id": trial_id, "message": "trial_id is not unique"})
        seen.add(trial_id)
        if not row["candidate_id"]:
            issues.append({"code": "MISSING_CANDIDATE_ID", "severity": "ERROR", "trial_id": trial_id, "message": "candidate_id is empty"})
        if not row["family"]:
            issues.append({"code": "MISSING_FAMILY", "severity": "ERROR", "trial_id": trial_id, "message": "family is empty"})
        if row["selected"] and row["selection_evidence_role"] != "development":
            issues.append(
                {
                    "code": "NON_DEVELOPMENT_SELECTION",
                    "severity": "ERROR",
                    "trial_id": trial_id,
                    "message": f"selected using evidence role {row['selection_evidence_role'] or '<missing>'}",
                }
            )
        if row["manual_override"]:
            manual_overrides.append(
                {
                    "trial_id": trial_id,
                    "candidate_id": row["candidate_id"],
                    "selected": row["selected"],
                    "selection_evidence_role": row["selection_evidence_role"],
                }
            )

        training = row["training_metric"]
        development = row["development_metric"]
        evaluation = row["evaluation_metric"]
        if training is not None and development is not None:
            gap = training - development if metric_direction == "higher" else development - training
            train_development_gaps.append(gap)
        if development is not None and evaluation is not None:
            gap = development - evaluation if metric_direction == "higher" else evaluation - development
            development_evaluation_gaps.append(gap)

    timeline_rows = sorted(
        (row for row in rows if row["event_order"] is not None and row["final_n"] is not None),
        key=lambda row: (row["event_order"], row["trial_id"]),
    )
    final_n_timeline: list[dict[str, Any]] = []
    previous: int | None = None
    for row in timeline_rows:
        if row["final_n"] != previous:
            final_n_timeline.append(
                {"event_order": row["event_order"], "trial_id": row["trial_id"], "final_n": row["final_n"]}
            )
            previous = row["final_n"]

    succeeded = sum(row["status"] in SUCCESS_STATES for row in rows)
    failed = sum(row["status"] in FAILURE_STATES for row in rows)
    other = len(rows) - succeeded - failed
    return {
        "selection_audit_verdict": "FAILED" if any(issue["severity"] == "ERROR" for issue in issues) else "PASSED",
        "trial_denominator": {
            "attempted": len(rows),
            "succeeded": succeeded,
            "failed": failed,
            "other_status": other,
        },
        "candidate_selection_frequency": _frequencies(rows, "candidate_id"),
        "family_stability_frequency": _frequencies(rows, "family"),
        "training_development_overfit_gap": _summary(train_development_gaps),
        "development_evaluation_deterioration": _summary(development_evaluation_gaps),
        "final_n_timeline": final_n_timeline,
        "manual_override_events": manual_overrides,
        "issues": issues,
        "threshold_verdict": "INCONCLUSIVE",
        "threshold_note": "Apply preregistered materiality thresholds externally.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", required=True, help="CSV containing the documented trial fields")
    parser.add_argument("--metric-direction", choices=("higher", "lower"), default="higher")
    parser.add_argument("--output", help="JSON output path; stdout when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = read_trials(args.trials)
        result = audit_model_selection(rows, args.metric_direction)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 1 if result["selection_audit_verdict"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
