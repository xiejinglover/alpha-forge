from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from portfolio_optimization_core import (
    ContractError,
    load_study,
    performance_metrics,
    risk_metrics,
    select_fixed_schemes,
)


class CoreTests(unittest.TestCase):
    def test_beta_alpha_and_linear_portfolio_identity(self) -> None:
        base = [-0.02, -0.01, 0.005, 0.01, 0.02, -0.015, 0.012, -0.004] * 4
        benchmark = [value * (1 + index * 0.001) for index, value in enumerate(base)]
        first = [0.001 + 0.2 * value for value in benchmark]
        second = [0.0005 + 0.8 * value for value in benchmark]
        portfolio = [(a + b) / 2 for a, b in zip(first, second)]
        first_metrics, _ = risk_metrics(first, benchmark, [[] for _ in benchmark], 252, 2)
        second_metrics, _ = risk_metrics(second, benchmark, [[] for _ in benchmark], 252, 2)
        portfolio_metrics, _ = risk_metrics(portfolio, benchmark, [[] for _ in benchmark], 252, 2)
        self.assertAlmostEqual(first_metrics["ordinary_beta"], 0.2, places=10)
        self.assertAlmostEqual(second_metrics["ordinary_beta"], 0.8, places=10)
        for key in ("ordinary_beta", "controlled_beta", "downside_beta", "tail_10_beta"):
            self.assertAlmostEqual(
                portfolio_metrics[key], (first_metrics[key] + second_metrics[key]) / 2, places=10
            )
        self.assertTrue(math.isnan(portfolio_metrics["tail_5_beta"]))
        self.assertGreater(first_metrics["residual_alpha"], 0)

    def test_alpha_poor_series_is_not_made_good_by_low_beta(self) -> None:
        benchmark = [-0.02, -0.01, 0.005, 0.01, 0.02, -0.015, 0.012, -0.004] * 4
        weak = [-0.001 + 0.05 * value for value in benchmark]
        metrics, _ = risk_metrics(weak, benchmark, [[] for _ in benchmark], 252, 1)
        self.assertLess(metrics["ordinary_beta"], 0.1)
        self.assertLess(metrics["residual_alpha"], 0)

    def test_stable_low_beta_selection_uses_beta_then_id_only(self) -> None:
        rows = [
            {"candidate_id": "B", "robust_quality_score": 2, "ordinary_beta": .2},
            {"candidate_id": "A", "robust_quality_score": 2, "ordinary_beta": .2},
            {"candidate_id": "C", "robust_quality_score": 1, "ordinary_beta": .1},
        ]
        selected = select_fixed_schemes(rows, 2)
        self.assertEqual(set(selected), {"LOW_BETA_EQ"})
        self.assertEqual(selected["LOW_BETA_EQ"], ["C", "A"])

    def test_study_rejects_overlap_and_consumed_independent_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "study.json"
            base = {
                "family_id": "family", "annualization": 252, "quality_top_k": 100,
                "quality_method": "ema20_robust_specific_quality_v1",
                "n_values": [2], "hac_lags": 1,
                "deployments": [{
                    "deployment_id": "x",
                    "estimation": {"start": "2024-01-01", "end": "2024-12-31"},
                    "evaluation": {"start": "2024-12-31", "end": "2025-12-31"},
                    "benchmark_id": "b", "evidence_role": "consumed_research",
                    "consumed_for_selection": True,
                    "evaluation_dataset_id": "eval-x", "evaluation_accessed_at": "2026-01-01T00:00:00",
                }],
            }
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(ContractError):
                load_study(path)
            base["deployments"][0]["evaluation"]["start"] = "2025-01-01"
            base["deployments"][0]["evidence_role"] = "independent_holdout"
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(ContractError):
                load_study(path)

    def test_study_rejects_raw_sharpe_as_quality_method(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "study.json"
            path.write_text(json.dumps({
                "family_id": "family", "annualization": 252, "quality_top_k": 100,
                "quality_method": "raw_sharpe", "n_values": [3], "hac_lags": 1,
                "deployments": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "raw Sharpe is not allowed"):
                load_study(path)

    def test_performance_handles_zero_volatility(self) -> None:
        metrics = performance_metrics([0.0, 0.0, 0.0], 252)
        self.assertTrue(math.isnan(metrics["sharpe"]))
        self.assertEqual(metrics["max_drawdown"], 0.0)


if __name__ == "__main__":
    unittest.main()
