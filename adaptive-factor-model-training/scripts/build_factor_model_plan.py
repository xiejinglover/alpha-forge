#!/usr/bin/env python3
"""Freeze confirmed factor routes, folds, model parameters, and seeds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from factor_model_core import (
    ContractError,
    environment_manifest,
    file_sha256,
    plan_trials,
    prepare_output_dir,
    read_json,
    read_jsonl,
    validate_confirmed_spec,
    validate_routes,
    write_json,
    write_jsonl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--confirmed-spec", required=True)
    parser.add_argument("--route-manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        profile_dir = Path(args.profile_dir).resolve()
        recommended_path = profile_dir / "recommended_spec.json"
        data_profile_path = profile_dir / "data_profile.json"
        for path in (recommended_path, data_profile_path):
            if not path.is_file():
                raise ContractError(f"profile directory lacks {path.name}")
        recommended_hash = file_sha256(recommended_path)
        spec = read_json(args.confirmed_spec)
        validate_confirmed_spec(spec, recommended_hash)
        routes = read_jsonl(args.route_manifest)
        validate_routes(routes)
        trials = plan_trials(spec, routes)
        output = prepare_output_dir(args.output_dir)
        frozen = {
            "schema": "adaptive-factor-model-training/frozen-v1",
            "recommended_spec_hash": recommended_hash,
            "confirmed_spec": spec,
            "routes": routes,
            "trial_count": len(trials),
            "automatic_parameter_search": False,
            "automatic_winner_selection": False,
            "environment": environment_manifest(),
        }
        write_json(output / "frozen_spec.json", frozen)
        write_jsonl(output / "route_plan.jsonl", routes)
        write_jsonl(output / "training_plan.jsonl", trials)
        write_json(output / "decision_log.json", {
            "confirmation_note": spec.get("confirmation_note", ""),
            "confirmed_at": spec.get("confirmed_at"),
            "overrides": spec.get("overrides", []),
            "recommended_spec_hash": recommended_hash,
        })
        manifest = {
            "schema": "adaptive-factor-model-training/plan-input-v1",
            "profile_dir": str(profile_dir),
            "data_profile_sha256": file_sha256(data_profile_path),
            "recommended_spec_sha256": recommended_hash,
            "confirmed_spec": str(Path(args.confirmed_spec).resolve()),
            "confirmed_spec_sha256": file_sha256(args.confirmed_spec),
            "route_manifest": str(Path(args.route_manifest).resolve()),
            "route_manifest_sha256": file_sha256(args.route_manifest),
        }
        write_json(output / "input_manifest.json", manifest)
        print(json.dumps({"output_dir": str(output), "routes": len(routes), "trials": len(trials)}))
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"plan build failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
