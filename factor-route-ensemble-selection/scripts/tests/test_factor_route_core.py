from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from factor_route_core import (  # noqa: E402
    ContractError,
    LineageData,
    build_tournament_groups,
    cluster_routes,
    expand_axis,
    nested_farthest_points,
    route_distance_matrix,
    signal_similarity,
)


class GridTests(unittest.TestCase):
    def test_decimal_range_is_exact_and_inclusive(self) -> None:
        self.assertEqual(
            expand_axis("fdr_q", {"start": "0.05", "end": "0.065", "step": "0.005"}),
            ["0.05", "0.055", "0.06", "0.065"],
        )

    def test_forbidden_model_axis_fails(self) -> None:
        with self.assertRaisesRegex(ContractError, "forbidden"):
            expand_axis("max_depth", {"values": [3, 5]})

    def test_range_must_land_on_end(self) -> None:
        with self.assertRaisesRegex(ContractError, "exactly"):
            expand_axis("fdr_q", {"start": "0", "end": "1", "step": "0.3"})


class StructureTests(unittest.TestCase):
    def setUp(self) -> None:
        sets = {
            ("A", "2023"): ("f1", "f2"),
            ("B", "2023"): ("f1", "f2"),
            ("C", "2023"): ("f3",),
            ("A", "2024"): ("f1", "f2"),
            ("B", "2024"): ("f1", "f4"),
            ("C", "2024"): ("f3",),
        }
        self.lineage = LineageData(
            factor_sets=sets,
            factor_hashes={key: "hash" for key in sets},
            complete_candidates=("A", "B", "C"),
            failures=(),
        )

    def test_mean_annual_jaccard_distance(self) -> None:
        ids, matrix = route_distance_matrix(self.lineage, ["2023", "2024"])
        index = {candidate_id: position for position, candidate_id in enumerate(ids)}
        self.assertAlmostEqual(matrix[index["A"], index["B"]], (0.0 + 2 / 3) / 2)
        self.assertEqual(matrix[index["A"], index["C"]], 1.0)

    def test_clustering_and_nested_sets_are_deterministic(self) -> None:
        ids, matrix = route_distance_matrix(self.lineage, ["2023", "2024"])
        medoids, mapping = cluster_routes(ids, matrix, 2)
        self.assertEqual(len(medoids), 2)
        self.assertEqual(len(mapping), 3)
        medoid_ids = [row["medoid_candidate_id"] for row in medoids]
        nested, coverage = nested_farthest_points(medoid_ids, ids, matrix, [1, 2])
        self.assertEqual([row["route_set_id"] for row in coverage], ["L1", "L2"])
        l1 = {row["candidate_id"] for row in nested if row["route_set_id"] == "L1"}
        l2 = {row["candidate_id"] for row in nested if row["route_set_id"] == "L2"}
        self.assertTrue(l1 < l2)


class SelectionTests(unittest.TestCase):
    def test_groups_are_shared_and_deterministic(self) -> None:
        first = build_tournament_groups(["A", "B", "C"], ["2023"], [1, 2], 5, 2026)
        second = build_tournament_groups(["C", "B", "A"], ["2023"], [1, 2], 5, 2026)
        self.assertEqual(first, second)
        self.assertEqual(len(first), (1 + 2) * 5)

    def test_overlap_threshold_boundary_can_be_checked_strictly(self) -> None:
        left = {
            __import__("datetime").date(2024, 1, 1): frozenset({"A", "B", "C", "D", "E"})
        }
        right = {
            __import__("datetime").date(2024, 1, 1): frozenset({"A", "B", "C", "X", "Y"})
        }
        self.assertAlmostEqual(signal_similarity(left, right), 0.6)
        self.assertFalse(signal_similarity(left, right) > 0.6)


if __name__ == "__main__":
    unittest.main()
