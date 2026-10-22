from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from factor_model_core import (  # noqa: E402
    ContractError,
    plan_trials,
    recommend_spec,
    validate_confirmed_spec,
    validate_routes,
)


class RecommendationTests(unittest.TestCase):
    def test_sparse_profile_is_conservative(self) -> None:
        profile = {"n_rows": 10_000, "n_decisions": 200, "n_features": 12}
        result = recommend_spec(profile, {})
        self.assertEqual(result["data_regime"], "sparse")
        self.assertEqual(result["model"]["params"]["max_depth"], 3)
        self.assertEqual(result["model"]["params"]["subsample"], 1.0)
        self.assertEqual(result["model"]["params"]["colsample_bytree"], 1.0)

    def test_large_profile_changes_capacity(self) -> None:
        profile = {"n_rows": 300_000, "n_decisions": 1200, "n_features": 140}
        result = recommend_spec(profile, {"task_type": "cross_sectional_ranking"})
        self.assertEqual(result["data_regime"], "large")
        self.assertEqual(result["model"]["params"]["max_rounds"], 3000)
        self.assertEqual(result["model"]["params"]["colsample_bytree"], 0.7)


class ContractTests(unittest.TestCase):
    def base_spec(self) -> dict:
        return {
            "user_confirmed": True,
            "recommended_spec_hash": "abc",
            "task_type": "cross_sectional_ranking",
            "model": {"family": "tree", "params": {}},
            "outer_folds": [{
                "fold_id": "2025", "train_start": "2022-01-01", "train_end": "2024-12-31",
                "validation_start": "2025-01-01", "validation_end": "2025-12-31",
            }],
            "seeds": [11, 13],
            "preprocessing": {"missing": "zero"},
            "model_adapter": {"command": ["python3", "adapter.py", "{trial_json}"]},
        }

    def test_confirmation_and_plan_denominator(self) -> None:
        spec = self.base_spec()
        validate_confirmed_spec(spec, "abc")
        routes = [
            {"route_id": "raw", "screening_recipe_id": "r1", "method": "raw"},
            {"route_id": "avg", "screening_recipe_id": "r1", "method": "hierarchical_average"},
        ]
        validate_routes(routes)
        trials = plan_trials(spec, routes)
        self.assertEqual(len(trials), 4)
        self.assertEqual(len({row["trial_id"] for row in trials}), 4)

    def test_rejects_unconfirmed_or_bad_fold(self) -> None:
        spec = self.base_spec()
        spec["user_confirmed"] = False
        with self.assertRaises(ContractError):
            validate_confirmed_spec(spec, "abc")
        spec = self.base_spec()
        spec["outer_folds"][0]["validation_start"] = "2024-01-01"
        with self.assertRaises(ContractError):
            validate_confirmed_spec(spec, "abc")


if __name__ == "__main__":
    unittest.main()
