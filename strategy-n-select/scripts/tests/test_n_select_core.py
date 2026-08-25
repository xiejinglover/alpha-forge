from __future__ import annotations

import json
import math
import csv
from datetime import date
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from n_select_core import (  # noqa: E402
    ContractError,
    load_study_config,
    read_development_signals,
    run_decorrelation,
    run_selection,
    run_voting,
    signal_overlap_similarity,
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
        self.assertTrue(all("metric_score" in row for row in members))

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
        signals = {"2026-01-05": {"S1": ["A"], "S2": ["B"], "S3": ["B"]}}
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
            {"2026-01-05": {"S1": ["B"], "S2": ["A"]}},
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
            {"2026-01-05": {"S1": ["ZZZ"], "S2": ["AAA"]}},
            [1],
        )
        unique = [row for row in asset_votes if row["member_vote_mode"] == "unique_equal"]
        self.assertEqual([row["asset_id"] for row in unique], ["AAA", "ZZZ"])

    def test_each_native_strategy_asset_receives_the_full_member_vote(self) -> None:
        member_votes, _, asset_votes, _ = run_voting(
            {4: {"S1": 5, "S2": 1}},
            {"2026-01-05": {"S1": ["A", "B", "C", "D"], "S2": ["D"]}},
            [1],
        )
        slot_member_rows = [
            row
            for row in member_votes
            if row["member_vote_mode"] == "slot_weighted" and row["candidate_id"] == "S1"
        ]
        self.assertEqual(len(slot_member_rows), 4)
        self.assertTrue(all(row["member_asset_count"] == 4 for row in slot_member_rows))
        self.assertTrue(all(row["vote_weight"] == 5 for row in slot_member_rows))

        slot_assets = [row for row in asset_votes if row["member_vote_mode"] == "slot_weighted"]
        self.assertEqual(
            [(row["asset_id"], row["votes"]) for row in slot_assets],
            [("D", 6), ("A", 5), ("B", 5), ("C", 5)],
        )
        unique_assets = [row for row in asset_votes if row["member_vote_mode"] == "unique_equal"]
        self.assertEqual(
            [(row["asset_id"], row["votes"]) for row in unique_assets],
            [("D", 2), ("A", 1), ("B", 1), ("C", 1)],
        )


class DecorrelationTests(unittest.TestCase):
    @staticmethod
    def signals(mapping: dict[str, list[str]]) -> dict[str, dict[str, frozenset[str]]]:
        return {
            candidate_id: {
                "2024-01-05": frozenset(assets),
                "2024-02-05": frozenset(assets),
            }
            for candidate_id, assets in mapping.items()
        }

    def test_overlap_similarity_and_strict_threshold(self) -> None:
        signals = self.signals(
            {
                "A": ["1", "2", "3", "4", "5"],
                "B": ["1", "2", "3", "6", "7"],
                "C": ["1", "2", "3", "4", "8"],
            }
        )
        similarity, dates = signal_overlap_similarity(signals["A"], signals["B"])
        self.assertEqual(dates, 2)
        self.assertAlmostEqual(similarity, 0.6)

        members = {
            10: [
                {"candidate_id": "A", "metric_score": 10.0, "win_count": 3},
                {"candidate_id": "B", "metric_score": 9.0, "win_count": 2},
                {"candidate_id": "C", "metric_score": 8.0, "win_count": 1},
            ]
        }
        _, trace, decorrelated, summaries = run_decorrelation(members, signals, "max", 0.6)
        self.assertEqual([row["candidate_id"] for row in decorrelated], ["A", "B"])
        self.assertEqual([row["win_count"] for row in decorrelated], [3, 2])
        self.assertAlmostEqual(sum(float(row["slot_weight"]) for row in decorrelated), 1.0)
        c_trace = next(row for row in trace if row["candidate_id"] == "C")
        self.assertEqual(c_trace["decision"], "removed")
        self.assertEqual(c_trace["blocking_candidate_id"], "A")
        self.assertEqual(summaries[0]["slots_removed"], 1)

    def test_min_direction_and_greedy_compares_only_with_kept_pool(self) -> None:
        signals = self.signals(
            {
                "A": ["1", "2", "3", "4", "5"],
                "B": ["1", "2", "3", "4", "6"],
                "C": ["1", "2", "6", "7", "8"],
            }
        )
        members = {
            5: [
                {"candidate_id": "A", "metric_score": 1.0, "win_count": 1},
                {"candidate_id": "B", "metric_score": 2.0, "win_count": 1},
                {"candidate_id": "C", "metric_score": 3.0, "win_count": 1},
            ]
        }
        _, _, decorrelated, _ = run_decorrelation(members, signals, "min", 0.6)
        self.assertEqual([row["candidate_id"] for row in decorrelated], ["A", "C"])

    def test_top_one_similarity_reduces_to_same_or_different_asset(self) -> None:
        left = {"2024-01-05": frozenset({"A"}), "2024-02-05": frozenset({"B"})}
        right = {"2024-01-05": frozenset({"A"}), "2024-02-05": frozenset({"C"})}
        similarity, dates = signal_overlap_similarity(left, right)
        self.assertEqual(dates, 2)
        self.assertEqual(similarity, 0.5)

    def test_metric_tie_uses_candidate_id(self) -> None:
        signals = self.signals({"A": ["1"], "B": ["1"]})
        members = {
            3: [
                {"candidate_id": "B", "metric_score": 1.0, "win_count": 2},
                {"candidate_id": "A", "metric_score": 1.0, "win_count": 1},
            ]
        }
        _, trace, decorrelated, _ = run_decorrelation(members, signals, "max", 0.6)
        self.assertEqual([row["candidate_id"] for row in decorrelated], ["A"])
        self.assertEqual(trace[1]["blocking_candidate_id"], "A")

    def test_development_signal_contract_rejects_calendar_and_k_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "signals.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["rebalance_date", "candidate_id", "asset_id"]
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"rebalance_date": "2024-01-05", "candidate_id": "A", "asset_id": "1"},
                        {"rebalance_date": "2024-01-05", "candidate_id": "A", "asset_id": "2"},
                        {"rebalance_date": "2024-01-05", "candidate_id": "B", "asset_id": "1"},
                    ]
                )
            with self.assertRaisesRegex(ContractError, "same asset count"):
                read_development_signals(
                    path,
                    date(2024, 1, 1),
                    date(2024, 12, 31),
                    {"A", "B"},
                )

    def test_development_signal_contract_rejects_calendar_mismatch_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calendar_path = Path(temp_dir) / "calendar.csv"
            with calendar_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["rebalance_date", "candidate_id", "asset_id"]
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"rebalance_date": "2024-01-05", "candidate_id": "A", "asset_id": "1"},
                        {"rebalance_date": "2024-02-05", "candidate_id": "A", "asset_id": "1"},
                        {"rebalance_date": "2024-01-05", "candidate_id": "B", "asset_id": "2"},
                    ]
                )
            with self.assertRaisesRegex(ContractError, "calendar mismatch"):
                read_development_signals(
                    calendar_path, date(2024, 1, 1), date(2024, 12, 31), {"A", "B"}
                )

            duplicate_path = Path(temp_dir) / "duplicate.csv"
            with duplicate_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["rebalance_date", "candidate_id", "asset_id"]
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"rebalance_date": "2024-01-05", "candidate_id": "A", "asset_id": "1"},
                        {"rebalance_date": "2024-01-05", "candidate_id": "A", "asset_id": "1"},
                    ]
                )
            with self.assertRaisesRegex(ContractError, "duplicates"):
                read_development_signals(
                    duplicate_path, date(2024, 1, 1), date(2024, 12, 31), {"A"}
                )


if __name__ == "__main__":
    unittest.main()
