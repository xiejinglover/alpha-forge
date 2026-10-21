from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from factor_evidence_core import ContractError, assign_groups, bh_qvalues, hac_mean, spearman  # noqa: E402
from run_factor_evidence import apply_fdr_and_consensus  # noqa: E402


class StatisticalCoreTests(unittest.TestCase):
    def test_bh_known_vector_is_monotone_and_mapped_back(self) -> None:
        values = [0.01, 0.04, 0.03, 0.002, None]
        result = bh_qvalues(values)
        self.assertAlmostEqual(result[0], 0.02)
        self.assertAlmostEqual(result[1], 0.04)
        self.assertAlmostEqual(result[2], 0.04)
        self.assertAlmostEqual(result[3], 0.008)
        self.assertIsNone(result[4])

    def test_hac_white_noise_and_invalid_lag(self) -> None:
        rng = np.random.default_rng(7)
        values = rng.normal(0.02, 0.1, size=500)
        result = hac_mean(values, lag=4, confidence=0.95)
        self.assertEqual(result["n"], 500)
        self.assertAlmostEqual(result["mean"], float(np.mean(values)))
        self.assertGreater(result["se"], 0)
        with self.assertRaises(ContractError):
            hac_mean(values[:5], lag=5, confidence=0.95)

    def test_ties_are_not_split_and_binary_requires_both_values(self) -> None:
        values = pd.Series([1, 1, 1, 2, 2, 3, 3, 3])
        groups = assign_groups(values, "continuous", 3)
        self.assertEqual(groups[values.eq(1)].nunique(), 1)
        binary = assign_groups(pd.Series([0, 0, 1, 1]), "binary", 2)
        self.assertEqual(set(binary), {1.0, 2.0})
        invalid = assign_groups(pd.Series([1, 1, 1]), "binary", 2)
        self.assertTrue(invalid.isna().all())

    def test_spearman_uses_average_ranks(self) -> None:
        value = spearman(pd.Series([1, 1, 2, 3]), pd.Series([1, 2, 3, 4]))
        self.assertTrue(math.isfinite(value))
        self.assertGreater(value, 0.9)

    def test_multi_window_consensus_rejects_opposite_directions(self) -> None:
        summaries = []
        plan = []
        for recipe_id, direction in [("short", 1), ("long", -1)]:
            recipe = {
                "fdr_alpha": 0.05,
                "minimum_consensus_windows": 2,
                "require_consistent_direction": True,
            }
            plan.append({"recipe_id": recipe_id, "recipe": recipe})
            summaries.append({
                "deployment_id": "d", "pool_id": "p", "recipe_id": recipe_id,
                "consensus_id": "c", "label_id": "l", "factor_id": "f", "primary_metric": "rank_ic",
                "primary_p": 0.001, "primary_q": None, "direction": direction,
                "model_candidate": True, "economic_pass": True, "stability_pass": True,
                "window_economic_eligible": False, "disposition": "pending_fdr",
            })
        lineage = apply_fdr_and_consensus(summaries, plan)
        self.assertEqual(len(lineage), 1)
        self.assertFalse(lineage[0]["direction_consistent"])
        self.assertFalse(lineage[0]["economic_eligible"])


if __name__ == "__main__":
    unittest.main()
