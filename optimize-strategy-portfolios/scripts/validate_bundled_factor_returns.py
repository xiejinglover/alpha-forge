#!/usr/bin/env python3
"""Validate a bundled wide control-factor return CSV against its manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSET = (
    SKILL_ROOT / "assets/risk-data/ema20_control_factor_returns_asof_2026-08-31.csv"
)
DEFAULT_MANIFEST = DEFAULT_ASSET.with_suffix(".manifest.json")


class ValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(asset: Path, manifest_path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read manifest {manifest_path}: {exc}") from exc
    expected_columns = manifest.get("columns")
    snapshot = manifest.get("snapshot")
    if not isinstance(expected_columns, list) or not isinstance(snapshot, dict):
        raise ValidationError("manifest requires columns and snapshot")
    if manifest.get("asset_file") != asset.name:
        raise ValidationError("manifest asset_file does not match CSV filename")
    if asset.stat().st_size != int(snapshot["size_bytes"]):
        raise ValidationError("CSV size does not match manifest")
    digest = sha256_file(asset)
    if digest != snapshot["sha256"]:
        raise ValidationError("CSV SHA-256 does not match manifest")

    row_count = 0
    first_day: date | None = None
    previous_day: date | None = None
    with asset.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_columns:
            raise ValidationError("CSV columns or column order do not match manifest")
        factors = expected_columns[1:]
        for line, row in enumerate(reader, 2):
            try:
                day = date.fromisoformat(row["trade_date"])
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"line {line}: invalid trade_date") from exc
            if previous_day is not None and day <= previous_day:
                raise ValidationError(f"line {line}: trade_date is duplicate or unsorted")
            for factor in factors:
                try:
                    value = float(row[factor])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"line {line}: {factor} is not numeric") from exc
                if not math.isfinite(value):
                    raise ValidationError(f"line {line}: {factor} is not finite")
            first_day = first_day or day
            previous_day = day
            row_count += 1
    if row_count != int(snapshot["row_count"]):
        raise ValidationError("CSV row count does not match manifest")
    if first_day is None or previous_day is None:
        raise ValidationError("CSV contains no rows")
    if first_day.isoformat() != snapshot["start_trade_date"]:
        raise ValidationError("CSV start date does not match manifest")
    if previous_day.isoformat() != snapshot["end_trade_date"]:
        raise ValidationError("CSV end date does not match manifest")
    if previous_day.isoformat() != snapshot["as_of_trade_date"]:
        raise ValidationError("CSV end date does not match snapshot cutoff")
    return {
        "status": "valid",
        "asset": str(asset.resolve()),
        "rows": row_count,
        "start": first_day.isoformat(),
        "end": previous_day.isoformat(),
        "factors": len(expected_columns) - 1,
        "sha256": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", type=Path, default=DEFAULT_ASSET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        result = validate(args.asset, args.manifest)
    except (ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
