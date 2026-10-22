#!/usr/bin/env python3
"""Execute a frozen model-training denominator through a repository adapter."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from factor_model_core import (
    ContractError,
    file_sha256,
    format_command,
    prepare_output_dir,
    read_json,
    read_jsonl,
    write_csv,
    write_json,
)


STATUS_COLUMNS = ["trial_id", "fold_id", "route_id", "seed", "status", "return_code", "output_dir", "reason"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_dir = Path(args.plan_dir).resolve()
    try:
        frozen_path = plan_dir / "frozen_spec.json"
        plan_path = plan_dir / "training_plan.jsonl"
        if not frozen_path.is_file() or not plan_path.is_file():
            raise ContractError("plan directory must contain frozen_spec.json and training_plan.jsonl")
        frozen = read_json(frozen_path)
        trials = read_jsonl(plan_path)
        expected = int(frozen.get("trial_count", -1))
        if expected != len(trials):
            raise ContractError(f"training plan denominator drifted: expected {expected}, found {len(trials)}")
        spec = frozen["confirmed_spec"]
        template = spec["model_adapter"]["command"]
        output = prepare_output_dir(args.output_dir)
        trial_root = output / "trials"
        trial_root.mkdir()
        statuses: list[dict[str, object]] = []
        stopped = False
        for trial in trials:
            trial_id = trial["trial_id"]
            trial_dir = trial_root / trial_id
            if stopped:
                statuses.append({**trial, "status": "not_run_after_failure", "return_code": "", "output_dir": str(trial_dir), "reason": "fail_fast"})
                continue
            trial_dir.mkdir()
            trial_json = trial_dir / "trial.json"
            write_json(trial_json, trial)
            command = format_command(template, {
                "trial_json": str(trial_json),
                "trial_output_dir": str(trial_dir),
                "plan_dir": str(plan_dir),
                "study_output_dir": str(output),
            })
            result = subprocess.run(command, cwd=spec.get("adapter_workdir"), text=True, capture_output=True, check=False)
            (trial_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
            (trial_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
            required_any = [trial_dir / "predictions.csv", trial_dir / "predictions.parquet"]
            required_all = [trial_dir / "metrics.json", trial_dir / "model_manifest.json", trial_dir / "SUCCESS.json"]
            if result.returncode == 0 and any(path.is_file() for path in required_any) and all(path.is_file() for path in required_all):
                status, reason = "completed", ""
            else:
                missing = [path.name for path in required_all if not path.is_file()]
                if not any(path.is_file() for path in required_any):
                    missing.append("predictions.csv|predictions.parquet")
                status = "failed"
                reason = f"return_code={result.returncode}; missing={','.join(missing)}"
                if args.fail_fast:
                    stopped = True
            statuses.append({
                "trial_id": trial_id,
                "fold_id": trial["fold_id"],
                "route_id": trial["route_id"],
                "seed": trial["seed"],
                "status": status,
                "return_code": result.returncode,
                "output_dir": str(trial_dir),
                "reason": reason,
            })
        write_csv(output / "trial_status.csv", statuses, STATUS_COLUMNS)
        failures = [row for row in statuses if row["status"] != "completed"]
        write_csv(output / "failure_records.csv", failures, STATUS_COLUMNS)
        prediction_manifest = output / "prediction_manifest.json"
        write_json(prediction_manifest, {
            "schema": "adaptive-factor-model-training/predictions-v1",
            "members": [
                {
                    "trial_id": row["trial_id"],
                    "fold_id": row["fold_id"],
                    "route_id": row["route_id"],
                    "seed": row["seed"],
                    "trial_output_dir": row["output_dir"],
                }
                for row in statuses if row["status"] == "completed"
            ],
            "ensemble_sizes": spec.get("ensemble_sizes", []),
            "ensemble_method": spec.get("ensemble_method", "within_decision_percentile_rank_mean"),
            "require_complete_member_denominator": True,
        })
        account_template = spec.get("account_adapter", {}).get("command")
        account_configured = bool(account_template)
        account_status = "not_configured"
        account_reason = "formal account adapter is absent"
        account_dir = output / "account"
        if account_configured and not failures:
            account_dir.mkdir()
            strategy_spec = output / "strategy_spec.json"
            write_json(strategy_spec, spec.get("strategy", {}))
            command = format_command(account_template, {
                "prediction_manifest": str(prediction_manifest),
                "strategy_spec": str(strategy_spec),
                "account_output_dir": str(account_dir),
                "plan_dir": str(plan_dir),
                "study_output_dir": str(output),
            })
            result = subprocess.run(command, cwd=spec.get("adapter_workdir"), text=True, capture_output=True, check=False)
            (account_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
            (account_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
            required_account = [account_dir / "metrics.json", account_dir / "engine_manifest.json", account_dir / "SUCCESS.json"]
            if result.returncode == 0 and all(path.is_file() for path in required_account):
                account_status, account_reason = "completed", ""
            else:
                missing = [path.name for path in required_account if not path.is_file()]
                account_status = "failed"
                account_reason = f"return_code={result.returncode}; missing={','.join(missing)}"
        elif account_configured:
            account_status = "not_run_due_to_model_failures"
            account_reason = "one or more frozen model trials failed"
        account_rows = [{"status": account_status, "output_dir": str(account_dir), "reason": account_reason}]
        write_csv(output / "account_run_manifest.csv", account_rows, ["status", "output_dir", "reason"])
        completed = len(statuses) - len(failures)
        report = [
            "# Factor model training report", "",
            f"- Planned trials: {len(trials)}",
            f"- Completed trials: {completed}",
            f"- Failed or not-run trials: {len(failures)}",
            f"- Formal account adapter configured: {str(account_configured).lower()}",
            f"- Formal account validation: {account_status}",
            "- Automatic parameter search: false",
            "- Automatic winner selection: false", "",
        ]
        (output / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
        output_names = ["trial_status.csv", "failure_records.csv", "prediction_manifest.json", "account_run_manifest.csv", "REPORT.md"]
        if failures:
            overall_status = "completed_with_failures"
        elif account_status == "completed":
            overall_status = "complete_account_validated"
        else:
            overall_status = "model_trials_complete_account_incomplete"
        success = {
            "schema": "adaptive-factor-model-training/run-v1",
            "status": overall_status,
            "planned_trials": len(trials),
            "completed_trials": completed,
            "failed_trials": len(failures),
            "account_status": account_status,
            "plan_sha256": file_sha256(plan_path),
            "outputs": {name: file_sha256(output / name) for name in output_names},
            "automatic_winner_selected": False,
        }
        write_json(output / "SUCCESS.json", success)
        print(json.dumps({"output_dir": str(output), "status": success["status"], "completed": completed, "failures": len(failures)}))
        return 0 if not failures else 2
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"training run failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
