from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[1]
BUILDER = SCRIPT_DIR / "build_factor_plan.py"
RUNNER = SCRIPT_DIR / "run_factor_evidence.py"


def strategy_contract(holding: int = 5, interval: int = 1) -> dict:
    return {
        "decision_schedule": "every_trading_day" if interval == 1 else "every_5_trading_days",
        "decision_interval_observations": interval,
        "entry_rule": "next_open",
        "exit_rule": "holding_close",
        "holding_horizon_observations": holding,
        "execution_delay_observations": 1,
        "cost_model": "production_v1",
        "require_net_of_costs": True,
        "overlapping_positions": holding > interval,
    }


def config(contract: dict | None = None) -> dict:
    contract = contract or strategy_contract()
    return {
        "study_id": "synthetic-5d",
        "timezone": "Asia/Shanghai",
        "thresholds_confirmed": True,
        "deployments": [
            {
                "id": "deploy-1",
                "information_cutoff": "2024-04-30T23:59:59+08:00",
                "windows": {"long": "2024-01-01T00:00:00+08:00"},
            }
        ],
        "recipes": [
            {
                "id": "rankic-5d-long",
                "consensus_id": "rankic-5d",
                "label_id": "net_return_5d",
                "window_id": "long",
                "primary_metric": "rank_ic",
                "strategy_contract": contract,
                "preferred_group_count": 5,
                "fallback_group_count": 3,
                "minimum_pool_size": 20,
                "minimum_group_size": 3,
                "require_all_groups": True,
                "factor_non_null_ratio": 0.8,
                "valid_decision_coverage": 0.7,
                "minimum_valid_decisions": 20,
                "valid_decision_cap": 60,
                "fdr_alpha": 0.05,
                "confidence_level": 0.95,
                "hac_primary_lag": "auto",
                "hac_sensitivity_lags": [8],
                "minimum_effect": 0.001,
                "positive_period_share": 0.6,
                "monotonicity_threshold": 0.6,
                "minimum_consensus_windows": 1,
                "require_consistent_direction": True,
            }
        ],
    }


class CliEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = self.make_data()
        self.config_path = self.root / "study.json"
        self.config_path.write_text(json.dumps(config()), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_data(self) -> dict[str, Path]:
        rng = np.random.default_rng(2026)
        decisions = pd.bdate_range("2024-01-02", periods=50, tz="Asia/Shanghai")
        observations = []
        factors = []
        exposures = []
        instruments = [f"S{i:02d}" for i in range(30)]
        for date_index, decision in enumerate(decisions):
            cross_noise = rng.normal(0, 0.001, len(instruments))
            for instrument_index, instrument in enumerate(instruments):
                observation_id = f"{date_index}-{instrument}"
                good = instrument_index / 29 + 0.02 * math_sin(date_index + instrument_index)
                label = 0.012 * good + cross_noise[instrument_index]
                observations.append({
                    "observation_id": observation_id,
                    "pool_id": "main",
                    "decision_time": decision.isoformat(),
                    "instrument_id": instrument,
                    "label_id": "net_return_5d",
                    "label_value": label,
                    "label_exit_time": (decision + pd.offsets.BDay(5)).isoformat(),
                })
                for factor_id, value in [
                    ("good", good),
                    ("noise", rng.normal()),
                    ("binary", float(instrument_index >= 15)),
                    ("sector", f"sector-{instrument_index % 3}"),
                ]:
                    factors.append({
                        "observation_id": observation_id,
                        "factor_id": factor_id,
                        "factor_value": value,
                        "factor_known_at": decision.isoformat(),
                    })
                exposures.append({
                    "observation_id": observation_id,
                    "exposure_id": "size",
                    "exposure_type": "continuous",
                    "exposure_value": instrument_index,
                    "exposure_known_at": decision.isoformat(),
                })
        metadata = [
            {"factor_id": "good", "factor_type": "continuous"},
            {"factor_id": "noise", "factor_type": "continuous"},
            {"factor_id": "binary", "factor_type": "binary"},
            {"factor_id": "sector", "factor_type": "nominal"},
        ]
        labels = [{
            "label_id": "net_return_5d",
            "decision_schedule": "every_trading_day",
            "decision_interval_observations": 1,
            "entry_rule": "next_open",
            "exit_rule": "holding_close",
            "holding_horizon_observations": 5,
            "execution_delay_observations": 1,
            "cost_model": "production_v1",
            "label_is_net_of_costs": True,
            "overlapping_positions": True,
        }]
        paths = {}
        for name, rows in [
            ("observations", observations), ("factors", factors), ("factor_metadata", metadata),
            ("labels", labels), ("exposures", exposures),
        ]:
            path = self.root / f"{name}.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            paths[name] = path
        return paths

    def run_command(self, script: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(script), *args], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, expected, msg=result.stderr or result.stdout)
        return result

    def build(self, plan_dir: Path, config_path: Path | None = None, expected: int = 0) -> subprocess.CompletedProcess:
        return self.run_command(
            BUILDER,
            "--config", str(config_path or self.config_path),
            "--observations", str(self.paths["observations"]),
            "--factors", str(self.paths["factors"]),
            "--factor-metadata", str(self.paths["factor_metadata"]),
            "--labels", str(self.paths["labels"]),
            "--exposures", str(self.paths["exposures"]),
            "--output-dir", str(plan_dir),
            expected=expected,
        )

    def test_end_to_end_horizon_hac_outputs_and_hash_guard(self) -> None:
        plan_dir = self.root / "plan"
        self.build(plan_dir)
        frozen = json.loads((plan_dir / "frozen_spec.json").read_text())
        recipe = frozen["recipes"][0]
        self.assertEqual(recipe["overlap_lag_floor"], 4)
        self.assertEqual(recipe["resolved_hac_primary_lag"], 4)
        output = self.root / "output"
        self.run_command(
            RUNNER,
            "--plan-dir", str(plan_dir),
            "--observations", str(self.paths["observations"]),
            "--factors", str(self.paths["factors"]),
            "--factor-metadata", str(self.paths["factor_metadata"]),
            "--labels", str(self.paths["labels"]),
            "--exposures", str(self.paths["exposures"]),
            "--output-dir", str(output),
        )
        with (output / "factor_summary.csv").open(encoding="utf-8") as handle:
            summaries = {row["factor_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(summaries["good"]["window_economic_eligible"], "true")
        self.assertEqual(summaries["sector"]["disposition"], "nominal_model_candidate_only")
        with (output / "period_factor_lineage.csv").open(encoding="utf-8") as handle:
            lineage = {row["factor_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(lineage["sector"]["model_candidate"], "true")
        self.assertEqual(lineage["sector"]["economic_eligible"], "false")
        success = json.loads((output / "SUCCESS.json").read_text())
        self.assertFalse(success["automatic_winner_selected"])

        with self.paths["factors"].open("a", encoding="utf-8") as handle:
            handle.write("\n")
        rejected = self.run_command(
            RUNNER,
            "--plan-dir", str(plan_dir),
            "--observations", str(self.paths["observations"]),
            "--factors", str(self.paths["factors"]),
            "--factor-metadata", str(self.paths["factor_metadata"]),
            "--labels", str(self.paths["labels"]),
            "--exposures", str(self.paths["exposures"]),
            "--output-dir", str(self.root / "rejected"),
            expected=2,
        )
        self.assertIn("input changed since plan freeze", rejected.stderr)

    def test_rejects_one_day_label_for_five_day_strategy(self) -> None:
        bad_labels = pd.read_csv(self.paths["labels"])
        bad_labels.loc[0, "holding_horizon_observations"] = 1
        bad_labels.loc[0, "overlapping_positions"] = False
        bad_path = self.root / "bad-labels.csv"
        bad_labels.to_csv(bad_path, index=False)
        original = self.paths["labels"]
        self.paths["labels"] = bad_path
        result = self.build(self.root / "bad-plan", expected=2)
        self.paths["labels"] = original
        self.assertIn("does not match strategy holding_horizon_observations", result.stderr)

    def test_rejects_future_factor_version(self) -> None:
        bad_factors = pd.read_csv(self.paths["factors"])
        decision = pd.Timestamp(bad_factors.loc[0, "factor_known_at"])
        bad_factors.loc[0, "factor_known_at"] = (decision + pd.Timedelta(days=1)).isoformat()
        bad_path = self.root / "future-factor.csv"
        bad_factors.to_csv(bad_path, index=False)
        original = self.paths["factors"]
        self.paths["factors"] = bad_path
        result = self.build(self.root / "future-plan", expected=2)
        self.paths["factors"] = original
        self.assertIn("factor_known_at is later than decision_time", result.stderr)

    def test_overlap_formula_for_non_overlapping_and_ten_day_holding(self) -> None:
        for holding, interval, expected in [(5, 5, 0), (10, 5, 1)]:
            labels = pd.read_csv(self.paths["labels"])
            labels.loc[0, "decision_schedule"] = "every_5_trading_days"
            labels.loc[0, "decision_interval_observations"] = interval
            labels.loc[0, "holding_horizon_observations"] = holding
            labels.loc[0, "overlapping_positions"] = holding > interval
            label_path = self.root / f"labels-{holding}-{interval}.csv"
            labels.to_csv(label_path, index=False)
            cfg = config(strategy_contract(holding=holding, interval=interval))
            cfg["recipes"][0]["hac_sensitivity_lags"] = [max(expected + 1, 2)]
            config_path = self.root / f"config-{holding}-{interval}.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            old = self.paths["labels"]
            self.paths["labels"] = label_path
            plan_dir = self.root / f"plan-{holding}-{interval}"
            self.build(plan_dir, config_path=config_path)
            self.paths["labels"] = old
            frozen = json.loads((plan_dir / "frozen_spec.json").read_text())
            self.assertEqual(frozen["recipes"][0]["resolved_hac_primary_lag"], expected)

    def test_future_data_does_not_change_historical_evidence(self) -> None:
        baseline_plan = self.root / "baseline-plan"
        self.build(baseline_plan)
        baseline_output = self.root / "baseline-output"
        self.run_command(
            RUNNER,
            "--plan-dir", str(baseline_plan),
            "--observations", str(self.paths["observations"]),
            "--factors", str(self.paths["factors"]),
            "--factor-metadata", str(self.paths["factor_metadata"]),
            "--labels", str(self.paths["labels"]),
            "--exposures", str(self.paths["exposures"]),
            "--output-dir", str(baseline_output),
        )

        future_paths = dict(self.paths)
        observations = pd.read_csv(self.paths["observations"])
        factors = pd.read_csv(self.paths["factors"])
        exposures = pd.read_csv(self.paths["exposures"])
        source_ids = set(observations.tail(30)["observation_id"])
        future_observations = observations[observations["observation_id"].isin(source_ids)].copy()
        id_map = {value: f"future-{value}" for value in future_observations["observation_id"]}
        future_observations["observation_id"] = future_observations["observation_id"].map(id_map)
        future_observations["decision_time"] = "2025-01-02T09:30:00+08:00"
        future_observations["label_exit_time"] = "2025-01-09T15:00:00+08:00"
        future_factors = factors[factors["observation_id"].isin(source_ids)].copy()
        future_factors["observation_id"] = future_factors["observation_id"].map(id_map)
        future_factors["factor_known_at"] = "2025-01-02T09:30:00+08:00"
        future_exposures = exposures[exposures["observation_id"].isin(source_ids)].copy()
        future_exposures["observation_id"] = future_exposures["observation_id"].map(id_map)
        future_exposures["exposure_known_at"] = "2025-01-02T09:30:00+08:00"
        for role, base, extra in [
            ("observations", observations, future_observations),
            ("factors", factors, future_factors),
            ("exposures", exposures, future_exposures),
        ]:
            path = self.root / f"with-future-{role}.csv"
            pd.concat([base, extra], ignore_index=True).to_csv(path, index=False)
            future_paths[role] = path

        original_paths = self.paths
        self.paths = future_paths
        future_plan = self.root / "future-data-plan"
        self.build(future_plan)
        future_output = self.root / "future-data-output"
        self.run_command(
            RUNNER,
            "--plan-dir", str(future_plan),
            "--observations", str(self.paths["observations"]),
            "--factors", str(self.paths["factors"]),
            "--factor-metadata", str(self.paths["factor_metadata"]),
            "--labels", str(self.paths["labels"]),
            "--exposures", str(self.paths["exposures"]),
            "--output-dir", str(future_output),
        )
        self.paths = original_paths
        for name in ["factor_summary.csv", "period_factor_lineage.csv", "daily_factor_metrics.csv"]:
            self.assertEqual((baseline_output / name).read_bytes(), (future_output / name).read_bytes())


def math_sin(value: float) -> float:
    return float(np.sin(value))


if __name__ == "__main__":
    unittest.main()
