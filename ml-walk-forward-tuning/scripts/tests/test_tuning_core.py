from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from tuning_core import ContractError, build_plan, validate_config  # noqa: E402


def make_config() -> dict:
    return {
        "study_id": "unit-study",
        "data": {
            "available_start": "2012-01-01",
            "available_end": "2019-12-31",
            "frequency": "daily",
            "calendar": "XSHG",
            "timezone": "Asia/Shanghai",
            "label_horizon_observations": 2,
            "purge_observations": 2,
            "embargo_observations": 0,
        },
        "development": {
            "validation_periods": [
                {"id": "2017", "start": "2017-01-01", "end": "2017-12-31"},
                {"id": "2018", "start": "2018-01-01", "end": "2018-12-31"},
                {"id": "2019", "start": "2019-01-01", "end": "2019-12-31"},
            ],
            "training_windows": [
                {"id": f"{years}y", "length": years, "unit": "calendar_years"}
                for years in range(1, 6)
            ],
        },
        "factor_sets": {"core": ["value", "quality"], "small": ["value"]},
        "fixed": {"selection_rule": "top1", "execution_rule": "next_open"},
        "search": {
            "factor_set": ["core", "small"],
            "label": ["return_2d", "return_5d"],
            "holding_rule": ["hold_2d", "hold_5d"],
        },
        "model_spaces": [
            {
                "id": "xgb",
                "family": "xgboost_ranker",
                "fixed_params": {"objective": "rank:pairwise"},
                "search_params": {"max_depth": [3, 5], "learning_rate": [0.03, 0.05]},
                "early_stopping": {"mode": "outer_validation", "rounds": 50},
            },
            {
                "id": "linear",
                "family": "ridge",
                "fixed_params": {"alpha": 1.0},
                "search_params": {},
                "early_stopping": {"mode": "disabled"},
            },
        ],
        "seeds": [11, 29],
        "metrics": [
            {"name": "rank_ic", "direction": "max"},
            {"name": "max_drawdown", "direction": "min"},
        ],
    }


class TuningCoreTests(unittest.TestCase):
    def test_full_product_time_windows_and_deterministic_ids(self) -> None:
        config = validate_config(make_config())
        candidates, trials, metadata = build_plan(config)
        self.assertEqual(metadata["candidate_count"], 5 * 2 * 2 * 2 * (4 + 1))
        self.assertEqual(metadata["trial_count"], len(candidates) * 3 * 2)

        one_year = next(
            trial
            for trial in trials
            if trial["candidate_config"]["training_window"]["id"] == "1y"
            and trial["validation_id"] == "2017"
        )
        five_year = next(
            trial
            for trial in trials
            if trial["candidate_config"]["training_window"]["id"] == "5y"
            and trial["validation_id"] == "2017"
        )
        self.assertEqual(one_year["training"]["start"], "2016-01-01")
        self.assertEqual(one_year["training"]["end"], "2016-12-31")
        self.assertEqual(five_year["training"]["start"], "2012-01-01")
        self.assertEqual(five_year["training"]["end"], "2016-12-31")

        candidates_again, trials_again, _ = build_plan(config)
        self.assertEqual(candidates, candidates_again)
        self.assertEqual(trials, trials_again)

    def test_model_parameters_do_not_cross_model_spaces(self) -> None:
        candidates, _, _ = build_plan(validate_config(make_config()))
        for candidate in candidates:
            model = candidate["config"]["model"]
            if model["space_id"] == "linear":
                self.assertEqual(model["params"], {"alpha": 1.0})
                self.assertNotIn("max_depth", model["params"])
            else:
                self.assertIn("max_depth", model["params"])
                self.assertNotIn("alpha", model["params"])

    def test_insufficient_history_fails_without_shortening(self) -> None:
        raw = make_config()
        raw["data"]["available_start"] = "2013-01-01"
        config = validate_config(raw)
        with self.assertRaisesRegex(ContractError, "before data.available_start"):
            build_plan(config)

    def test_purge_and_dimension_contracts(self) -> None:
        bad_purge = make_config()
        bad_purge["data"]["purge_observations"] = 1
        with self.assertRaisesRegex(ContractError, "must be >="):
            validate_config(bad_purge)

        overlap = make_config()
        overlap["fixed"]["label"] = "return_2d"
        with self.assertRaisesRegex(ContractError, "overlap"):
            validate_config(overlap)

        missing = make_config()
        del missing["search"]["holding_rule"]
        with self.assertRaisesRegex(ContractError, "missing required dimensions"):
            validate_config(missing)

    def test_unknown_factor_set_and_duplicate_search_value_fail(self) -> None:
        unknown = make_config()
        unknown["search"]["factor_set"] = ["unknown"]
        with self.assertRaisesRegex(ContractError, "unknown explicit set"):
            validate_config(unknown)

        duplicate = make_config()
        duplicate["search"]["label"] = ["return_2d", "return_2d"]
        with self.assertRaisesRegex(ContractError, "duplicate values"):
            validate_config(duplicate)

    def test_validation_overlap_and_development_bounds_fail(self) -> None:
        overlap = make_config()
        overlap["development"]["validation_periods"][1]["start"] = "2017-12-31"
        with self.assertRaisesRegex(ContractError, "ordered and non-overlapping"):
            validate_config(overlap)

        outside = make_config()
        outside["development"]["validation_periods"][-1]["end"] = "2020-01-01"
        with self.assertRaisesRegex(ContractError, "outside data coverage"):
            validate_config(outside)


if __name__ == "__main__":
    unittest.main()
