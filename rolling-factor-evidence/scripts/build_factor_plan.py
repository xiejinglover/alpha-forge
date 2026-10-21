#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from factor_evidence_core import (
    ContractError,
    canonical_json,
    file_sha256,
    load_inputs,
    prepare_output_dir,
    read_json,
    stable_id,
    validate_config,
    write_csv,
)


def choose_groups(pool_sizes: pd.Series, preferred: int, fallback: int, minimum_group_size: int) -> int:
    if pool_sizes.empty:
        return 0
    conservative_pool = int(np.quantile(pool_sizes.to_numpy(dtype=float), 0.10, method="lower"))
    if conservative_pool >= preferred * minimum_group_size:
        return preferred
    if conservative_pool >= fallback * minimum_group_size:
        return fallback
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an immutable rolling factor evidence plan")
    parser.add_argument("--config", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--factors", required=True)
    parser.add_argument("--factor-metadata", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--exposures")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        data = load_inputs(args.observations, args.factors, args.factor_metadata, args.labels, args.exposures)
        config = validate_config(read_json(args.config), data["labels"])
        output = prepare_output_dir(args.output_dir)

        observations = data["observations"]
        factors = data["factors"]
        metadata = data["metadata"]
        feasibility: list[dict] = []
        plan: list[dict] = []
        recipes = {recipe["id"]: recipe for recipe in config["recipes"]}
        factor_types = metadata.set_index("factor_id")["factor_type"].to_dict()

        for deployment in config["deployments"]:
            cutoff = pd.to_datetime(deployment["information_cutoff"], utc=True)
            for recipe in config["recipes"]:
                start = pd.to_datetime(deployment["windows"][recipe["window_id"]], utc=True)
                obs = observations[
                    observations["label_id"].eq(recipe["label_id"])
                    & observations["decision_time"].between(start, cutoff, inclusive="both")
                ].copy()
                for pool_id in sorted(obs["pool_id"].unique()):
                    pool_obs = obs[obs["pool_id"].eq(pool_id)]
                    pool_sizes = pool_obs.groupby("decision_time")["observation_id"].nunique()
                    structural_groups = choose_groups(
                        pool_sizes,
                        int(recipe["preferred_group_count"]),
                        int(recipe["fallback_group_count"]),
                        int(recipe["minimum_group_size"]),
                    )
                    for factor_id in metadata["factor_id"].sort_values():
                        factor = factors[factors["factor_id"].eq(factor_id)][["observation_id", "factor_value"]]
                        joined = pool_obs[["observation_id"]].merge(factor, on="observation_id", how="left")
                        non_null = int(joined["factor_value"].notna().sum())
                        rows = len(joined)
                        factor_type = factor_types[factor_id]
                        planned_groups = 2 if factor_type == "binary" else (0 if factor_type == "nominal" else structural_groups)
                        row = {
                        "study_id": config["study_id"],
                        "deployment_id": deployment["id"],
                        "pool_id": pool_id,
                        "recipe_id": recipe["id"],
                        "consensus_id": recipe["consensus_id"],
                        "label_id": recipe["label_id"],
                        "window_id": recipe["window_id"],
                        "window_start": start.isoformat(),
                        "information_cutoff": cutoff.isoformat(),
                        "factor_id": factor_id,
                        "factor_type": factor_type,
                        "planned_group_count": planned_groups,
                        "structural_observations": rows,
                        "structural_non_null": non_null,
                        "structural_non_null_ratio": non_null / rows if rows else math.nan,
                        "structural_decisions": int(len(pool_sizes)),
                        "pool_size_p10": int(np.quantile(pool_sizes, 0.10, method="lower")) if len(pool_sizes) else 0,
                        "pool_size_median": float(pool_sizes.median()) if len(pool_sizes) else math.nan,
                        }
                        identity = {key: row[key] for key in ["study_id", "deployment_id", "pool_id", "recipe_id", "label_id", "window_id", "factor_id"]}
                        row["plan_id"] = stable_id("factor", identity)
                        feasibility.append(row)
                        plan.append({**row, "recipe": recipes[recipe["id"]]})

        if not plan:
            raise ContractError("no plan rows were generated; check label, pool, deployment, and window coverage")

        frozen_spec = {**config, "config_sha256": file_sha256(args.config)}
        manifest_paths = {
            "config": args.config,
            "observations": args.observations,
            "factors": args.factors,
            "factor_metadata": args.factor_metadata,
            "labels": args.labels,
        }
        if args.exposures:
            manifest_paths["exposures"] = args.exposures
        manifest = {
            "study_id": config["study_id"],
            "inputs": {
                role: {"filename": Path(path).name, "sha256": file_sha256(path)}
                for role, path in manifest_paths.items()
            },
            "plan_rows": len(plan),
        }
        (output / "frozen_spec.json").write_text(json.dumps(frozen_spec, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "input_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with (output / "factor_plan.jsonl").open("w", encoding="utf-8") as handle:
            for row in plan:
                handle.write(canonical_json(row) + "\n")
        columns = [
            "plan_id", "study_id", "deployment_id", "pool_id", "recipe_id", "consensus_id", "label_id", "window_id",
            "window_start", "information_cutoff", "factor_id", "factor_type", "planned_group_count",
            "structural_observations", "structural_non_null", "structural_non_null_ratio",
            "structural_decisions", "pool_size_p10", "pool_size_median",
        ]
        write_csv(output / "structural_feasibility.csv", feasibility, columns)
        manifest["plan_artifacts"] = {
            name: file_sha256(output / name)
            for name in ["frozen_spec.json", "factor_plan.jsonl", "structural_feasibility.csv"]
        }
        (output / "input_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"built {len(plan)} frozen factor plan rows in {output}")
        return 0
    except (ContractError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
