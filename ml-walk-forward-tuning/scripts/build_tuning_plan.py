#!/usr/bin/env python3
"""Validate a study config and build a deterministic full tuning plan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tuning_core import (
    ContractError,
    build_plan,
    ensure_empty_output_dir,
    file_sha256,
    load_json,
    stable_hash,
    validate_config,
    write_json,
    write_jsonl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="study_config.json")
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = validate_config(load_json(args.config))
        candidates, trials, metadata = build_plan(config)
        ensure_empty_output_dir(args.output_dir)
        normalized_config = args.output_dir / "study_config.normalized.json"
        candidates_path = args.output_dir / "candidate_configs.jsonl"
        trials_path = args.output_dir / "trial_plan.jsonl"
        write_json(normalized_config, config)
        write_jsonl(candidates_path, candidates)
        write_jsonl(trials_path, trials)
        manifest = {
            "schema_version": 1,
            "study_id": config["study_id"],
            "development_end": config["development"]["validation_periods"][-1]["end"],
            "config_sha256": stable_hash(config),
            "artifacts": {
                "study_config.normalized.json": file_sha256(normalized_config),
                "candidate_configs.jsonl": file_sha256(candidates_path),
                "trial_plan.jsonl": file_sha256(trials_path),
            },
            "counts": metadata,
            "metrics": config["metrics"],
        }
        write_json(args.output_dir / "study_manifest.json", manifest)
        print(
            f"built {metadata['candidate_count']} candidates and {metadata['trial_count']} trials "
            f"in {args.output_dir}"
        )
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

