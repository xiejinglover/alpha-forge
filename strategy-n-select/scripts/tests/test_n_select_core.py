from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from n_select_core import (  # noqa: E402
    ContractError,
    load_study_config,
    run_selection,
    run_voting,
    verify_weight_sums,
)


class StudyConfigTests(unittest.TestCase):
    def test_requires_non_overlapping_periods_and_explicit_consumption_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "study.json"
            path.write_text(
                json.dumps(
                    {
                        "development": {"start": "2024-01-01", "end": "2024-12-31"},
                        "holdout": {
                            "start": "2024-12-31",
                            "end": "2025-12-31",
                            "consumed_for_selection": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ContractError, "overlap"):
                load_study_config(path)


class SelectionTests(unittest.TestCase):
    def test_selection_is_deterministic_and_ties_break_by_candidate_id(self) -> None:
        scores = {"S1": 1.0, "S2": 2.0, "S3": 2.0, "S4": -1.0}
        first = run_selection(scores, [1, 4], matches=5, seed=2026, direction="max")
        second = run_selection(scores, [1, 4], matches=5, seed=2026, direction="max")
        self.assertEqual(first, second)
        _, winners, members = first
        n4_winners = [row for row in winners if row["n"] == 4]
        self.assertTrue(all(row["winner_candidate_id"] == "S2" for row in n4_winners))
        self.assertEqual(sum(row["win_count"] for row in members if row["n"] == 4), 5)

    def test_min_direction_and_invalid_group(self) -> None:
        _, winners, members = run_selection(
            {"A": math.nan, "B": math.inf}, [1], matches=3, seed=1, direction="min"
        )
        self.assertTrue(all(row["status"] == "invalid_group" for row in winners))
        self.assertEqual(members, [])

        _, winners, _ = run_selection({"A": 2.0, "B": 1.0}, [2], matches=1, seed=1, direction="min")
        self.assertEqual(winners[0]["winner_candidate_id"], "B")

    def test_n_larger_than_candidate_pool_fails(self) -> None:
        with self.assertRaisesRegex(ContractError, "exceeds candidate count"):
            run_selection({"A": 1.0}, [2], matches=1, seed=1, direction="max")


class VotingTests(unittest.TestCase):
    def test_member_vote_modes_can_reverse_top_one(self) -> None:
        members = {5: {"S1": 5, "S2": 1, "S3": 1}}
        signals = {"2026-01-05": {"S1": "A", "S2": "B", "S3": "B"}}
        member_votes, missing, asset_votes, weights = run_voting(members, signals, [1, 3, 5])
        self.assertEqual(missing, [])
        self.assertEqual(len(member_votes), 6)

        slot = [row for row in asset_votes if row["member_vote_mode"] == "slot_weighted"]
        unique = [row for row in asset_votes if row["member_vote_mode"] == "unique_equal"]
        self.assertEqual([(row["asset_id"], row["votes"]) for row in slot], [("A", 5), ("B", 2)])
        self.assertEqual([(row["asset_id"], row["votes"]) for row in unique], [("B", 2), ("A", 1)])

        slot_top_one = [
            row
            for row in weights
            if row["member_vote_mode"] == "slot_weighted" and row["selection_label"] == "top_1"
        ]
        unique_top_one = [
            row
            for row in weights
            if row["member_vote_mode"] == "unique_equal" and row["selection_label"] == "top_1"
        ]
        self.assertEqual(slot_top_one[0]["asset_id"], "A")
        self.assertEqual(unique_top_one[0]["asset_id"], "B")
        self.assertEqual(float(slot_top_one[0]["target_weight"]), 1.0)
        self.assertEqual(float(unique_top_one[0]["target_weight"]), 1.0)
        verify_weight_sums(weights)

    def test_all_and_large_top_k_have_same_assets_and_vote_weights(self) -> None:
        _, _, _, weights = run_voting(
            {3: {"S1": 2, "S2": 1}},
            {"2026-01-05": {"S1": "B", "S2": "A"}},
            [5],
        )
        slot = [row for row in weights if row["member_vote_mode"] == "slot_weighted"]
        all_rows = [row for row in slot if row["selection_label"] == "all"]
        top_rows = [row for row in slot if row["selection_label"] == "top_5"]
        self.assertEqual(
            [(row["asset_id"], row["target_weight"]) for row in all_rows],
            [(row["asset_id"], row["target_weight"]) for row in top_rows],
        )

    def test_asset_ties_break_by_asset_id(self) -> None:
        _, _, asset_votes, _ = run_voting(
            {2: {"S1": 1, "S2": 1}},
            {"2026-01-05": {"S1": "ZZZ", "S2": "AAA"}},
            [1],
        )
        unique = [row for row in asset_votes if row["member_vote_mode"] == "unique_equal"]
        self.assertEqual([row["asset_id"] for row in unique], ["AAA", "ZZZ"])


if __name__ == "__main__":
    unittest.main()
