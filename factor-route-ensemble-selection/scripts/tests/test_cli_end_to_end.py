from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
BUILDER = SCRIPT_DIR / "build_factor_route_plan.py"
RUNNER = SCRIPT_DIR / "run_factor_route_study.py"
sys.path.insert(0, str(SCRIPT_DIR))

from factor_route_core import stable_hash  # noqa: E402


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "study.json"
        self.config.write_text(
            json.dumps(
                {
                    "study_id": "tiny-route-study",
                    "user_confirmed": True,
                    "upstream": {
                        "factor_evidence_hash": "factor-evidence-v1",
                        "label_spec_hash": "label-v1",
                        "preprocessing_hash": "prep-v1",
                        "model_spec_hash": "model-v1",
                        "account_spec_hash": "account-v1",
                        "data_hash": "data-v1",
                        "code_hash": "code-v1",
                    },
                    "deployment_periods": [
                        {
                            "id": "2023",
                            "information_cutoff": "2022-12-30",
                            "selection_start": "2020-01-01",
                            "selection_end": "2022-12-31",
                            "deployment_start": "2023-01-01",
                            "deployment_end": "2023-12-31",
                            "evidence_role": "development_consumed",
                        },
                        {
                            "id": "2024",
                            "information_cutoff": "2023-12-29",
                            "selection_start": "2021-01-01",
                            "selection_end": "2023-12-31",
                            "deployment_start": "2024-01-01",
                            "deployment_end": "2024-12-31",
                            "evidence_role": "diagnostic_consumed",
                        },
                    ],
                    "structural_search": {
                        "profile": "custom",
                        "axes": {
                            "fdr_q": {"values": ["0.1", "0.2"]},
                            "correlation_threshold": {"values": ["0.5", "0.8"]},
                        },
                    },
                    "compression": {"target_medoids": 2, "nested_sizes": [1, 2]},
                    "model": {"seeds": [11, 29]},
                    "fixed_ensembles": {
                        "route_equal_weight": 0.5,
                        "factor_set_equal_weight": 0.5,
                        "diagnostics": ["candidate_mass", "parameter_bin_equal", "median", "trimmed_mean", "consensus_penalty"],
                        "parameter_bins": {
                            "axes": ["fdr_q", "correlation_threshold"],
                            "counts": [2, 2],
                        },
                        "trim_fraction": 0.1,
                        "consensus_penalty": 0.25,
                    },
                    "selection": {
                        "lookback_complete_calendar_years": 3,
                        "metrics": [
                            {"name": "sharpe", "direction": "max"},
                            {"name": "calmar", "direction": "max"},
                            {"name": "positive_month_rate", "direction": "max"},
                        ],
                        "n_values": [1, 2],
                        "matches": 5,
                        "seed": 2026,
                        "overlap_threshold": 0.6,
                        "top_k": [1],
                    },
                    "statistics": {"annualization": 252, "hac_lag": 1, "fdr_alpha": 0.1},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_command(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, *args], cwd=SCRIPT_DIR, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, msg=result.stderr or result.stdout)
        return result

    def test_full_pipeline(self) -> None:
        plan = self.root / "plan"
        self.run_command(str(BUILDER), "--config", str(self.config), "--output-dir", str(plan))
        candidates = [json.loads(line) for line in (plan / "candidate_grid.jsonl").read_text().splitlines()]
        spec = json.loads((plan / "frozen_spec.json").read_text())
        lineage = self.root / "lineage.csv"
        factors_by_route = {
            candidate["candidate_id"]: (["f1", "f2"] if index < 2 else ["f3", f"f{index + 4}"])
            for index, candidate in enumerate(candidates)
        }
        lineage_rows = []
        for candidate in candidates:
            for period in spec["deployment_periods"]:
                factors = factors_by_route[candidate["candidate_id"]]
                factor_hash = stable_hash(factors)
                for order, factor_id in enumerate(factors, 1):
                    lineage_rows.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "deployment_id": period["id"],
                            "information_cutoff": period["information_cutoff"],
                            "factor_id": factor_id,
                            "factor_order": order,
                            "factor_set_hash": factor_hash,
                            "route_status": "completed",
                            "source_evidence_hash": "factor-evidence-v1",
                            "route_spec_hash": candidate["route_spec_hash"],
                        }
                    )
        write_csv(lineage, list(lineage_rows[0]), lineage_rows)
        structure = self.root / "structure"
        self.run_command(str(RUNNER), "structure", "--plan-dir", str(plan), "--lineage", str(lineage), "--output-dir", str(structure))
        with (structure / "medoids.csv").open() as handle:
            medoids = [row["medoid_candidate_id"] for row in csv.DictReader(handle)]
        self.assertEqual(len(medoids), 2)

        predictions = self.root / "predictions.csv"
        prediction_rows = [
            {
                "deployment_id": deployment_id,
                "decision_time": decision_time,
                "instrument_id": asset,
                "candidate_id": candidate_id,
                "score": asset_index + candidate_index * 0.1,
                "prediction_hash": f"p-{deployment_id}-{candidate_index}",
            }
            for deployment_id, decision_time in (("2023", "2023-06-01"), ("2024", "2024-06-03"))
            for candidate_index, candidate_id in enumerate(medoids)
            for asset_index, asset in enumerate(("A", "B", "C"), 1)
        ]
        write_csv(predictions, list(prediction_rows[0]), prediction_rows)
        fixed_ensembles = self.root / "fixed"
        self.run_command(str(RUNNER), "aggregate", "--plan-dir", str(plan), "--structure-dir", str(structure), "--predictions", str(predictions), "--output-dir", str(fixed_ensembles))
        with (fixed_ensembles / "fixed_ensemble_weights.csv").open() as handle:
            aggregators = {row["aggregator_id"] for row in csv.DictReader(handle)}
        self.assertIn("parameter_bin_equal", aggregators)

        returns = self.root / "returns.csv"
        return_rows = [
            {"date": day, "candidate_id": candidate_id, "net_return": value + candidate_index * 0.0002}
            for candidate_index, candidate_id in enumerate(medoids)
            for day, value in (
                ("2020-03-02", 0.001), ("2020-09-01", -0.0003),
                ("2021-03-01", 0.0012), ("2021-09-01", -0.0002),
                ("2022-03-01", 0.0011), ("2022-09-01", -0.0001),
                ("2023-03-01", 0.0009), ("2023-09-01", -0.0002),
            )
        ]
        write_csv(returns, ["date", "candidate_id", "net_return"], return_rows)
        signals = self.root / "signals.csv"
        signal_rows = []
        for candidate_index, candidate_id in enumerate(medoids):
            for day in ("2020-06-01", "2021-06-01", "2022-06-01", "2023-06-01", "2024-06-03"):
                assets = ("A", "B") if candidate_index == 0 else ("A", "C")
                signal_rows.extend(
                    {"rebalance_date": day, "candidate_id": candidate_id, "asset_id": asset}
                    for asset in assets
                )
        write_csv(signals, ["rebalance_date", "candidate_id", "asset_id"], signal_rows)
        selection = self.root / "selection"
        self.run_command(str(RUNNER), "select", "--plan-dir", str(plan), "--structure-dir", str(structure), "--member-returns", str(returns), "--member-signals", str(signals), "--output-dir", str(selection))

        fixed_accounts = self.root / "fixed_accounts.csv"
        selected_accounts = self.root / "selected_accounts.csv"
        account_days = ("2023-03-01", "2023-09-01", "2024-03-01", "2024-09-02")
        fixed_rows = [
            {"configuration_id": configuration_id, "date": day, "net_return": 0.001 + index * 0.00001, "continuous_account": "true", "account_hash": f"fixed-{configuration_id}"}
            for configuration_id in ("route_equal", "annual_factor_set_equal", "fixed_blend")
            for index, day in enumerate(account_days)
        ]
        write_csv(fixed_accounts, list(fixed_rows[0]), fixed_rows)
        with (selection / "target_weights.csv").open() as handle:
            configuration_ids = sorted({row["configuration_id"] for row in csv.DictReader(handle)})
        selected_rows = [
            {"configuration_id": configuration_id, "date": day, "net_return": 0.0011 + index * 0.00001, "continuous_account": "true", "account_hash": f"selected-{configuration_id}"}
            for configuration_id in configuration_ids
            for index, day in enumerate(account_days)
        ]
        write_csv(selected_accounts, list(selected_rows[0]), selected_rows)
        report = self.root / "report"
        self.run_command(str(RUNNER), "summarize", "--plan-dir", str(plan), "--fixed-accounts", str(fixed_accounts), "--selected-accounts", str(selected_accounts), "--output-dir", str(report))
        self.assertTrue((report / "SUCCESS.json").exists())
        self.assertIn("不自动选择冠军", (report / "REPORT.md").read_text())

    def test_unconfirmed_plan_cannot_run_and_output_is_not_overwritten(self) -> None:
        raw = json.loads(self.config.read_text())
        raw["user_confirmed"] = False
        unconfirmed_config = self.root / "unconfirmed.json"
        unconfirmed_config.write_text(json.dumps(raw), encoding="utf-8")
        plan = self.root / "unconfirmed-plan"
        self.run_command(
            str(BUILDER),
            "--config",
            str(unconfirmed_config),
            "--output-dir",
            str(plan),
        )
        self.assertTrue((plan / "recommended_spec.json").exists())
        self.assertFalse((plan / "frozen_spec.json").exists())
        rejected = self.run_command(
            str(BUILDER),
            "--config",
            str(unconfirmed_config),
            "--output-dir",
            str(plan),
            expected=2,
        )
        self.assertIn("not empty", rejected.stderr)

    def test_frozen_candidate_grid_tampering_is_rejected(self) -> None:
        plan = self.root / "tamper-plan"
        self.run_command(str(BUILDER), "--config", str(self.config), "--output-dir", str(plan))
        with (plan / "candidate_grid.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        rejected = self.run_command(
            str(RUNNER),
            "structure",
            "--plan-dir",
            str(plan),
            "--lineage",
            str(self.root / "unused.csv"),
            "--output-dir",
            str(self.root / "unused-output"),
            expected=2,
        )
        self.assertIn("Candidate grid hash", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
