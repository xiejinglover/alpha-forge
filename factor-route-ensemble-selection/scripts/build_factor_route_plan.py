#!/usr/bin/env python3
"""Build and optionally freeze a structural factor-route research plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from factor_route_core import (
    ContractError,
    SCHEMA_VERSION,
    build_candidates,
    ensure_empty_dir,
    read_json,
    sha256_file,
    stable_hash,
    validate_config,
    write_csv,
    write_json,
    write_jsonl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = validate_config(read_json(args.config))
        candidates = build_candidates(config)
        ensure_empty_dir(args.output_dir)

        recommended = {
            "schema_version": SCHEMA_VERSION,
            "study_id": config["study_id"],
            "profile": config["structural_search"].get("profile", "custom"),
            "candidate_count": len(candidates),
            "deployment_count": len(config["deployment_periods"]),
            "target_medoids": config["compression"]["target_medoids"],
            "route_equal_weight": config["fixed_ensembles"]["route_equal_weight"],
            "factor_set_equal_weight": config["fixed_ensembles"]["factor_set_equal_weight"],
            "n_values": config["selection"]["n_values"],
            "matches": config["selection"]["matches"],
            "confirmation_required": not config["user_confirmed"],
        }
        write_json(args.output_dir / "recommended_spec.json", recommended)

        grid_rows = [
            {
                "candidate_id": row["candidate_id"],
                "route_spec_hash": row["route_spec_hash"],
                "params_json": json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
            }
            for row in candidates
        ]
        write_csv(
            args.output_dir / "candidate_grid.csv",
            ["candidate_id", "route_spec_hash", "params_json"],
            grid_rows,
        )
        write_jsonl(args.output_dir / "candidate_grid.jsonl", candidates)

        lineage_plan = [
            {
                "candidate_id": candidate["candidate_id"],
                "route_spec_hash": candidate["route_spec_hash"],
                "params": candidate["params"],
                "deployment_id": period["id"],
                "information_cutoff": period["information_cutoff"],
                "status": "planned",
            }
            for candidate in candidates
            for period in config["deployment_periods"]
        ]
        write_jsonl(args.output_dir / "lineage_plan.jsonl", lineage_plan)

        seed_count = len(config["model"]["seeds"])
        workload = {
            "candidate_routes": len(candidates),
            "deployment_periods": len(config["deployment_periods"]),
            "lineage_tasks": len(lineage_plan),
            "maximum_medoid_annual_models": (
                config["compression"]["target_medoids"]
                * len(config["deployment_periods"])
                * seed_count
            ),
            "seed_count": seed_count,
            "tournament_group_rows": (
                len(config["deployment_periods"])
                * sum(config["selection"]["n_values"])
                * config["selection"]["matches"]
            ),
        }
        write_json(args.output_dir / "estimated_workload.json", workload)
        feasibility = [
            {"check": "candidate_count", "value": len(candidates), "status": "pass"},
            {
                "check": "target_medoids",
                "value": config["compression"]["target_medoids"],
                "status": "pass",
            },
            {
                "check": "maximum_n",
                "value": max(config["selection"]["n_values"]),
                "status": "pass",
            },
            {
                "check": "user_confirmation",
                "value": str(config["user_confirmed"]).lower(),
                "status": "pass" if config["user_confirmed"] else "pending",
            },
        ]
        write_csv(args.output_dir / "structural_feasibility.csv", ["check", "value", "status"], feasibility)

        if config["user_confirmed"]:
            frozen = {
                **config,
                "schema_version": SCHEMA_VERSION,
                "candidate_count": len(candidates),
                "plan_hash": stable_hash(
                    {
                        "config": config,
                        "candidates": candidates,
                        "lineage_plan": lineage_plan,
                    }
                ),
            }
            write_json(args.output_dir / "frozen_spec.json", frozen)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "stage": "route_plan_frozen",
                "source_config_sha256": sha256_file(args.config),
                "frozen_spec_sha256": sha256_file(args.output_dir / "frozen_spec.json"),
                "candidate_grid_sha256": sha256_file(args.output_dir / "candidate_grid.jsonl"),
                "lineage_plan_sha256": sha256_file(args.output_dir / "lineage_plan.jsonl"),
                "plan_hash": frozen["plan_hash"],
            }
            write_json(args.output_dir / "input_manifest.json", manifest)
        return 0
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
