#!/usr/bin/env python3
"""Audit ML time boundaries, OOF ownership, and locked-OOS access.

All time fields are integer positions on the same trading-session axis.  The
module has no third-party dependencies and can be imported or used as a CLI.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


SAMPLE_FIELDS = (
    "sample_id",
    "fold_id",
    "feature_cutoff",
    "decision_at",
    "label_end",
    "fit_as_of",
    "train_end",
    "validation_start",
    "label_horizon",
    "purge",
    "embargo",
    "in_training",
    "prediction_role",
    "prediction_owner",
)
LEDGER_FIELDS = (
    "dataset_id",
    "access_order",
    "purpose",
    "used_for_selection",
    "status",
)
DEVELOPMENT_ROLES = {"development_oof", "internal_walk_forward"}
SELECTION_PURPOSES = {
    "selection",
    "development_selection",
    "tuning",
    "elimination",
    "manual_readd",
}


def read_csv(path: str | Path, required_fields: Iterable[str]) -> list[dict[str, str]]:
    """Read a UTF-8 CSV and require the declared columns."""

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header")
        missing = [field for field in required_fields if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
        return [dict(row) for row in reader]


def _integer(value: Any, field: str, row_id: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{row_id}: {field} must be an integer") from exc


def _boolean(value: Any, field: str, row_id: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{row_id}: {field} must be true or false")


def _issue(code: str, row_id: str, message: str, severity: str = "ERROR") -> dict[str, str]:
    return {"code": code, "severity": severity, "row_id": row_id, "message": message}


def audit_samples(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate causal time order, purge/embargo, and OOF ownership."""

    issues: list[dict[str, str]] = []
    owner_groups: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    checked = 0

    for number, row in enumerate(rows, start=2):
        sample_id = str(row.get("sample_id", "")).strip() or f"line-{number}"
        row_id = f"sample:{sample_id}"
        try:
            feature_cutoff = _integer(row.get("feature_cutoff"), "feature_cutoff", row_id)
            decision_at = _integer(row.get("decision_at"), "decision_at", row_id)
            label_end = _integer(row.get("label_end"), "label_end", row_id)
            fit_as_of = _integer(row.get("fit_as_of"), "fit_as_of", row_id)
            train_end = _integer(row.get("train_end"), "train_end", row_id)
            validation_start = _integer(row.get("validation_start"), "validation_start", row_id)
            label_horizon = _integer(row.get("label_horizon"), "label_horizon", row_id)
            purge = _integer(row.get("purge"), "purge", row_id)
            embargo = _integer(row.get("embargo"), "embargo", row_id)
            in_training = _boolean(row.get("in_training"), "in_training", row_id)
        except ValueError as exc:
            issues.append(_issue("INVALID_VALUE", row_id, str(exc)))
            continue

        checked += 1
        role = str(row.get("prediction_role", "")).strip()
        owner = str(row.get("prediction_owner", "")).strip()
        fold_id = str(row.get("fold_id", "")).strip()
        case_id = str(row.get("case_id", "default")).strip() or "default"

        if feature_cutoff > decision_at:
            issues.append(_issue("FEATURE_AFTER_DECISION", row_id, "feature_cutoff exceeds decision_at"))
        if feature_cutoff > fit_as_of:
            issues.append(_issue("FEATURE_AFTER_FIT", row_id, "feature_cutoff exceeds fit_as_of"))
        if label_end > fit_as_of:
            issues.append(_issue("LABEL_AFTER_FIT", row_id, "label_end exceeds fit_as_of"))
        if label_horizon < 0 or purge < 0 or embargo < 0:
            issues.append(_issue("NEGATIVE_WINDOW", row_id, "horizon, purge, and embargo must be non-negative"))
        if purge < label_horizon:
            issues.append(_issue("PURGE_TOO_SHORT", row_id, "purge is shorter than label_horizon"))
        if embargo < label_horizon:
            issues.append(_issue("EMBARGO_TOO_SHORT", row_id, "embargo is shorter than label_horizon"))
        available_gap = validation_start - train_end - 1
        if available_gap < purge:
            issues.append(
                _issue(
                    "FOLD_GAP_TOO_SHORT",
                    row_id,
                    f"available train/validation gap {available_gap} is smaller than purge {purge}",
                )
            )
        if role in DEVELOPMENT_ROLES and in_training:
            issues.append(_issue("OOF_TRAINING_CONTAMINATION", row_id, "development prediction belongs to its model training set"))
        if not role:
            issues.append(_issue("MISSING_PREDICTION_ROLE", row_id, "prediction_role is empty"))
        if not owner:
            issues.append(_issue("MISSING_PREDICTION_OWNER", row_id, "prediction_owner is empty"))
        else:
            owner_groups[(case_id, fold_id, sample_id, role)].add(owner)

    for key, owners in sorted(owner_groups.items()):
        if len(owners) > 1:
            case_id, fold_id, sample_id, role = key
            issues.append(
                _issue(
                    "MULTIPLE_PREDICTION_OWNERS",
                    f"sample:{sample_id}",
                    f"case={case_id}, fold={fold_id}, role={role} has owners {sorted(owners)}",
                )
            )

    return {"checked_rows": checked, "issues": issues}


def audit_oos_ledger(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize locked-OOS state and flag selection or confirmation reuse."""

    issues: list[dict[str, str]] = []
    parsed: list[dict[str, Any]] = []
    for number, row in enumerate(rows, start=2):
        dataset_id = str(row.get("dataset_id", "")).strip() or f"line-{number}"
        row_id = f"ledger:{dataset_id}:{number}"
        try:
            access_order = _integer(row.get("access_order"), "access_order", row_id)
            used_for_selection = _boolean(row.get("used_for_selection"), "used_for_selection", row_id)
        except ValueError as exc:
            issues.append(_issue("INVALID_VALUE", row_id, str(exc)))
            continue
        parsed.append(
            {
                "dataset_id": dataset_id,
                "access_order": access_order,
                "purpose": str(row.get("purpose", "")).strip().lower(),
                "used_for_selection": used_for_selection,
                "declared_status": str(row.get("status", "")).strip().lower(),
                "source_row": number,
            }
        )

    normalized: list[dict[str, Any]] = []
    for dataset_id in sorted({row["dataset_id"] for row in parsed}):
        dataset_rows = sorted(
            (row for row in parsed if row["dataset_id"] == dataset_id),
            key=lambda item: (item["access_order"], item["source_row"]),
        )
        consumed = False
        confirmations = 0
        confirmed_before = False
        for row in dataset_rows:
            row_id = f"ledger:{dataset_id}:{row['access_order']}"
            purpose = row["purpose"]
            is_selection = row["used_for_selection"] or purpose in SELECTION_PURPOSES
            if purpose == "final_confirmation":
                confirmations += 1
                if consumed:
                    issues.append(_issue("CONFIRMATION_AFTER_SELECTION", row_id, "consumed OOS cannot provide final confirmation"))
                if confirmations > 1:
                    issues.append(_issue("REPEATED_FINAL_CONFIRMATION", row_id, "locked final OOS was used for confirmation more than once"))
                confirmed_before = True
            if is_selection:
                if confirmed_before:
                    issues.append(_issue("POST_CONFIRMATION_SELECTION", row_id, "selection occurred after final confirmation"))
                consumed = True
            effective_status = "consumed_for_selection" if consumed else (row["declared_status"] or "locked")
            if consumed and row["declared_status"] in {"locked", "final", "final_confirmation"}:
                issues.append(
                    _issue(
                        "STALE_LEDGER_STATUS",
                        row_id,
                        "declared status remains final/locked after selection; effective status was downgraded",
                        "WARNING",
                    )
                )
            normalized.append(
                {
                    "dataset_id": dataset_id,
                    "access_order": row["access_order"],
                    "purpose": purpose,
                    "used_for_selection": is_selection,
                    "declared_status": row["declared_status"],
                    "effective_status": effective_status,
                }
            )

    return {"checked_rows": len(parsed), "normalized_ledger": normalized, "issues": issues}


def audit_temporal_integrity(
    samples: Iterable[Mapping[str, Any]], ledger: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Run the complete deterministic temporal-integrity audit."""

    sample_result = audit_samples(samples)
    ledger_result = audit_oos_ledger(ledger)
    issues = sample_result["issues"] + ledger_result["issues"]
    has_error = any(issue["severity"] == "ERROR" for issue in issues)
    return {
        "eligibility_verdict": "FAILED" if has_error else "PASSED",
        "sample_rows_checked": sample_result["checked_rows"],
        "ledger_rows_checked": ledger_result["checked_rows"],
        "issues": issues,
        "normalized_ledger": ledger_result["normalized_ledger"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="CSV with temporal sample and fold fields")
    parser.add_argument("--ledger", required=True, help="CSV with locked-OOS access events")
    parser.add_argument("--output", help="JSON output path; stdout when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        samples = read_csv(args.samples, SAMPLE_FIELDS)
        ledger = read_csv(args.ledger, LEDGER_FIELDS)
        result = audit_temporal_integrity(samples, ledger)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 1 if result["eligibility_verdict"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
