#!/usr/bin/env python3
"""Profile a strategy training table and propose a user-confirmed starting spec."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from factor_model_core import (
    ContractError,
    file_sha256,
    prepare_output_dir,
    profile_csv,
    read_json,
    recommend_spec,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, help="CSV training table or adapter-produced profile table")
    parser.add_argument("--contract", required=True, help="JSON with columns and known strategy constraints")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = prepare_output_dir(args.output_dir)
        contract = read_json(args.contract)
        columns = contract.get("columns", {})
        decision = columns.get("decision_time")
        label = columns.get("label")
        features = columns.get("features")
        if not decision or not label or not isinstance(features, list) or not features:
            raise ContractError("contract.columns must define decision_time, label, and a non-empty features list")
        profile = profile_csv(
            args.dataset,
            decision_column=decision,
            label_column=label,
            feature_columns=features,
        )
        recommended = recommend_spec(profile, contract)
        write_json(output / "data_profile.json", profile)
        write_json(output / "strategy_profile.json", contract)
        write_json(output / "recommended_spec.json", recommended)
        questions = recommended["open_questions"] or ["No unresolved structural questions; confirm or override the recommendation."]
        lines = ["# Open questions", "", *[f"- {item}" for item in questions], ""]
        (output / "OPEN_QUESTIONS.md").write_text("\n".join(lines), encoding="utf-8")
        manifest = {
            "schema": "adaptive-factor-model-training/context-v1",
            "dataset": str(Path(args.dataset).resolve()),
            "dataset_sha256": file_sha256(args.dataset),
            "contract": str(Path(args.contract).resolve()),
            "contract_sha256": file_sha256(args.contract),
            "outputs": {
                name: file_sha256(output / name)
                for name in ["data_profile.json", "strategy_profile.json", "recommended_spec.json", "OPEN_QUESTIONS.md"]
            },
        }
        write_json(output / "input_manifest.json", manifest)
        print(json.dumps({"output_dir": str(output), "data_regime": recommended["data_regime"], "open_questions": len(questions)}))
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"context inspection failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
