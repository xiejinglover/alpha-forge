import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compute_stability_metrics import (
    average_ranks,
    compute_stability_metrics,
    pearson,
    quantile,
    spearman,
    top_k_assets,
)


def record(date, asset, case_id, seed, score, target):
    return {
        "date": date,
        "asset": asset,
        "case_id": case_id,
        "seed": seed,
        "score": score,
        "target": target,
    }


class StabilityMetricTests(unittest.TestCase):
    def test_quantile_and_average_tie_ranks(self):
        self.assertEqual(quantile([0.0, 10.0], 0.25), 2.5)
        self.assertEqual(average_ranks([10.0, 10.0, 20.0]), [1.5, 1.5, 3.0])
        self.assertAlmostEqual(spearman([1.0, 1.0, 2.0], [3.0, 3.0, 4.0]), 1.0)

    def test_constant_or_insufficient_correlation_is_none(self):
        self.assertIsNone(pearson([1.0], [1.0]))
        self.assertIsNone(pearson([1.0, 1.0], [2.0, 3.0]))

    def test_top_k_uses_all_available_when_universe_is_small(self):
        self.assertEqual(top_k_assets({"A": 2.0, "B": 1.0}, 10), {"A", "B"})

    def test_paired_metrics_handle_missing_and_different_universes(self):
        rows = [
            record("d1", "A", "base", "1", 3.0, 3.0),
            record("d1", "B", "base", "1", 2.0, 2.0),
            record("d1", "C", "base", "1", 1.0, 1.0),
            record("d2", "A", "base", "1", 2.0, 2.0),
            record("d2", "B", "base", "1", None, 1.0),
            record("d1", "A", "case", "2", 3.0, 3.0),
            record("d1", "B", "case", "2", 1.0, 2.0),
            record("d1", "D", "case", "2", 2.0, 1.0),
            record("d2", "A", "case", "2", 2.0, 2.0),
            record("d2", "B", "case", "2", 1.0, 1.0),
        ]
        result = compute_stability_metrics(rows, "base", "1", top_k=2)
        case = result["paired_vs_baseline"]["case::seed=2"]
        self.assertEqual(case["common_date_count"], 2)
        self.assertEqual(case["minimum_common_assets"], 1)
        self.assertIsNotNone(case["top_k_jaccard"]["median"])
        self.assertEqual(result["per_variant"]["base::seed=1"]["missing_score_count"], 1)
        self.assertEqual(result["threshold_verdict"], "INCONCLUSIVE")

    def test_zero_variance_rank_ic_yields_warning_not_crash(self):
        rows = [
            record("d1", "A", "base", "1", 1.0, 1.0),
            record("d1", "B", "base", "1", 1.0, 2.0),
        ]
        result = compute_stability_metrics(rows, "base", "1")
        self.assertEqual(result["per_variant"]["base::seed=1"]["daily_rank_ic"]["count"], 0)
        self.assertIn("NO_VALID_RANK_IC", {warning["code"] for warning in result["warnings"]})

    def test_ordered_perturbations_report_non_monotonic_points(self):
        rows = [
            record("d1", "A", "base", "1", 2.0, 2.0),
            record("d1", "B", "base", "1", 1.0, 1.0),
            record("d1", "A", "noise-low", "1", 1.0, 2.0),
            record("d1", "B", "noise-low", "1", 2.0, 1.0),
            record("d1", "A", "noise-high", "1", 2.0, 2.0),
            record("d1", "B", "noise-high", "1", 1.0, 1.0),
        ]
        order = ["base::seed=1", "noise-low::seed=1", "noise-high::seed=1"]
        result = compute_stability_metrics(rows, "base", "1", top_k=1, ordered_variants=order)
        violations = result["degradation_curve"]["daily_rank_ic_median"]["non_monotonic_points"]
        self.assertEqual(len(violations), 1)


if __name__ == "__main__":
    unittest.main()
