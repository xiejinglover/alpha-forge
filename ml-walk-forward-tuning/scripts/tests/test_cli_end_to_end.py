from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
BUILDER = SCRIPT_DIR / "build_tuning_plan.py"
SUMMARIZER = SCRIPT_DIR / "summarize_tuning_results.py"


def small_config() -> dict:
    return {
        "study_id": "e2e-study",
        "data": {
            "available_start": "2015-01-01",
            "available_end": "2018-12-31",
            "frequency": "daily",
            "calendar": "XSHG",
            "timezone": "Asia/Shanghai",
            "label_horizon_observations": 1,
            "purge_observations": 1,
            "embargo_observations": 0,
        },
        "development": {
            "validation_periods": [
                {"id": "2017", "start": "2017-01-01", "end": "2017-12-31"},
                {"id": "2018", "start": "2018-01-01", "end": "2018-12-31"},
            ],
            "training_windows": [{"id": "1y", "length": 1, "unit": "calendar_years"}],
        },
        "factor_sets": {"core": ["value"], "quality": ["quality"]},
        "fixed": {
            "label": "return_2d",
            "selection_rule": "top1",
            "holding_rule": "hold_2d",
            "execution_rule": "next_open",
        },
        "search": {"factor_set": ["core", "quality"]},
        "model_spaces": [
            {
                "id": "ridge",
                "family": "ridge",
                "fixed_params": {"alpha": 1.0},
                "search_params": {},
                "early_stopping": {"mode": "disabled"},
            }
        ],
        "seeds": [1, 2],
        "metrics": [
            {"name": "return", "direction": "max"},
            {"name": "drawdown", "direction": "min"},
        ],
    }


def write_results(path: Path, rows: list[dict]) -> None:
    fields = [
        "trial_id",
        "candidate_id",
        "validation_id",
        "seed",
        "metric",
        "value",
        "status",
        "artifact_path",
        "error",
        "actual_iterations",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class CliEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "study.json"
        self.config_path.write_text(json.dumps(small_config()), encoding="utf-8")
        self.plan_dir = self.root / "plan"
        self.run_command(
            str(BUILDER), "--config", str(self.config_path), "--output-dir", str(self.plan_dir)
        )
        self.trials = [json.loads(line) for line in (self.plan_dir / "trial_plan.jsonl").read_text().splitlines()]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_command(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, *args], cwd=SCRIPT_DIR, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, expected, msg=result.stderr or result.stdout)
        return result

    def completed_rows(self, trials: list[dict]) -> list[dict]:
        rows = []
        for trial in trials:
            candidate_offset = 0.2 if trial["candidate_config"]["dimensions"]["factor_set"] == "core" else 0.1
            year_offset = 0.01 if trial["validation_id"] == "2018" else 0.0
            seed_offset = trial["seed"] * 0.001
            values = {
                "return": candidate_offset + year_offset + seed_offset,
                "drawdown": 0.1 if candidate_offset == 0.2 else 0.2,
            }
            for metric, value in values.items():
                rows.append(
                    {
                        "trial_id": trial["trial_id"],
                        "candidate_id": trial["candidate_id"],
                        "validation_id": trial["validation_id"],
                        "seed": trial["seed"],
                        "metric": metric,
                        "value": value,
                        "status": "completed",
                        "artifact_path": f"artifacts/{trial['trial_id']}",
                        "error": "",
                        "actual_iterations": "",
                    }
                )
        return rows

    def test_complete_summary_rankings_pareto_and_hash_guard(self) -> None:
        results = self.root / "results.csv"
        write_results(results, self.completed_rows(self.trials))
        output = self.root / "summary"
        self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(self.plan_dir),
            "--results",
            str(results),
            "--output-dir",
            str(output),
        )
        with (output / "completeness.csv").open(encoding="utf-8") as handle:
            completeness = list(csv.DictReader(handle))
        self.assertTrue(all(row["complete"] == "true" for row in completeness))
        with (output / "pareto_candidates.csv").open(encoding="utf-8") as handle:
            pareto = list(csv.DictReader(handle))
        self.assertEqual(len(pareto), 1)
        self.assertIn('"factor_set":"core"', pareto[0]["candidate_config_json"])
        report = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn("不自动选择或冻结配置", report)
        manifest = json.loads((output / "summary_manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["automatic_winner_selected"])
        self.assertFalse(manifest["configuration_frozen"])

        with (self.plan_dir / "trial_plan.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        rejected = self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(self.plan_dir),
            "--results",
            str(results),
            "--output-dir",
            str(self.root / "rejected"),
            expected=2,
        )
        self.assertIn("frozen plan artifact changed", rejected.stderr)

    def test_failed_and_missing_trials_remain_in_denominator(self) -> None:
        first_candidate = self.trials[0]["candidate_id"]
        first_candidate_trials = [trial for trial in self.trials if trial["candidate_id"] == first_candidate]
        second_candidate_trials = [trial for trial in self.trials if trial["candidate_id"] != first_candidate]
        rows = self.completed_rows(first_candidate_trials)
        failed_trial = second_candidate_trials[0]
        rows.append(
            {
                "trial_id": failed_trial["trial_id"],
                "candidate_id": failed_trial["candidate_id"],
                "validation_id": failed_trial["validation_id"],
                "seed": failed_trial["seed"],
                "metric": "",
                "value": "",
                "status": "failed",
                "artifact_path": "",
                "error": "training failed",
                "actual_iterations": "",
            }
        )
        results = self.root / "partial-results.csv"
        write_results(results, rows)
        output = self.root / "partial-summary"
        self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(self.plan_dir),
            "--results",
            str(results),
            "--output-dir",
            str(output),
        )
        with (output / "completeness.csv").open(encoding="utf-8") as handle:
            completeness = {row["candidate_id"]: row for row in csv.DictReader(handle)}
        second_id = failed_trial["candidate_id"]
        self.assertEqual(completeness[first_candidate]["complete"], "true")
        self.assertEqual(completeness[second_id]["complete"], "false")
        self.assertEqual(completeness[second_id]["failed_trials"], "1")
        self.assertGreater(int(completeness[second_id]["missing_trials"]), 0)
        with (output / "pareto_candidates.csv").open(encoding="utf-8") as handle:
            pareto_ids = {row["candidate_id"] for row in csv.DictReader(handle)}
        self.assertEqual(pareto_ids, {first_candidate})
        with (output / "all_results.csv").open(encoding="utf-8") as handle:
            statuses = {row["status"] for row in csv.DictReader(handle)}
        self.assertEqual(statuses, {"completed", "failed", "missing"})

    def test_unknown_duplicate_and_partial_metric_results_fail(self) -> None:
        base_rows = self.completed_rows([self.trials[0]])
        unknown = [dict(base_rows[0], trial_id="trial_unknown")]
        unknown_path = self.root / "unknown.csv"
        write_results(unknown_path, unknown)
        rejected = self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(self.plan_dir),
            "--results",
            str(unknown_path),
            "--output-dir",
            str(self.root / "unknown-output"),
            expected=2,
        )
        self.assertIn("unknown trial_id", rejected.stderr)

        duplicate_path = self.root / "duplicate.csv"
        write_results(duplicate_path, [base_rows[0], base_rows[0]])
        rejected = self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(self.plan_dir),
            "--results",
            str(duplicate_path),
            "--output-dir",
            str(self.root / "duplicate-output"),
            expected=2,
        )
        self.assertIn("duplicates or conflicts", rejected.stderr)

        partial_path = self.root / "partial.csv"
        write_results(partial_path, [base_rows[0]])
        rejected = self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(self.plan_dir),
            "--results",
            str(partial_path),
            "--output-dir",
            str(self.root / "partial-output"),
            expected=2,
        )
        self.assertIn("must report every metric", rejected.stderr)

    def test_early_stopping_requires_actual_iterations(self) -> None:
        config = small_config()
        config["model_spaces"][0]["early_stopping"] = {"mode": "outer_validation", "rounds": 10}
        config_path = self.root / "early.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        plan_dir = self.root / "early-plan"
        self.run_command(str(BUILDER), "--config", str(config_path), "--output-dir", str(plan_dir))
        trial = json.loads((plan_dir / "trial_plan.jsonl").read_text().splitlines()[0])
        rows = []
        for metric in ("return", "drawdown"):
            rows.append(
                {
                    "trial_id": trial["trial_id"],
                    "candidate_id": trial["candidate_id"],
                    "validation_id": trial["validation_id"],
                    "seed": trial["seed"],
                    "metric": metric,
                    "value": 0.1,
                    "status": "completed",
                    "artifact_path": "",
                    "error": "",
                    "actual_iterations": "",
                }
            )
        results = self.root / "early-results.csv"
        write_results(results, rows)
        rejected = self.run_command(
            str(SUMMARIZER),
            "--plan-dir",
            str(plan_dir),
            "--results",
            str(results),
            "--output-dir",
            str(self.root / "early-output"),
            expected=2,
        )
        self.assertIn("must record actual_iterations", rejected.stderr)


if __name__ == "__main__":
    unittest.main()

