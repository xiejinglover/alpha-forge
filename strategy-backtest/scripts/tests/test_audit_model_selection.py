import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_model_selection import audit_model_selection


def trial(trial_id, **overrides):
    row = {
        "trial_id": trial_id,
        "candidate_id": "c1",
        "family": "small-tree",
        "status": "success",
        "training_metric": 0.30,
        "development_metric": 0.20,
        "evaluation_metric": 0.10,
        "selected": False,
        "manual_override": False,
        "event_order": 1,
        "final_n": 2,
        "selection_evidence_role": "development",
    }
    row.update(overrides)
    return row


class ModelSelectionTests(unittest.TestCase):
    def test_denominator_gaps_and_final_n_changes(self):
        rows = [
            trial("t1", selected=True, final_n=2, event_order=1),
            trial("t2", candidate_id="c2", status="failed", training_metric=None, development_metric=None, evaluation_metric=None, final_n=4, event_order=2),
        ]
        result = audit_model_selection(rows)
        self.assertEqual(result["trial_denominator"], {"attempted": 2, "succeeded": 1, "failed": 1, "other_status": 0})
        self.assertAlmostEqual(result["training_development_overfit_gap"]["mean"], 0.10)
        self.assertAlmostEqual(result["development_evaluation_deterioration"]["mean"], 0.10)
        self.assertEqual([event["final_n"] for event in result["final_n_timeline"]], [2, 4])

    def test_locked_oos_selection_and_manual_override_are_exposed(self):
        result = audit_model_selection(
            [
                trial(
                    "t1",
                    selected=True,
                    manual_override=True,
                    selection_evidence_role="locked_final_oos",
                )
            ]
        )
        self.assertEqual(result["selection_audit_verdict"], "FAILED")
        self.assertIn("NON_DEVELOPMENT_SELECTION", {issue["code"] for issue in result["issues"]})
        self.assertEqual(len(result["manual_override_events"]), 1)

    def test_duplicate_trial_id_fails(self):
        result = audit_model_selection([trial("same"), trial("same", candidate_id="c2")])
        self.assertIn("DUPLICATE_TRIAL_ID", {issue["code"] for issue in result["issues"]})

    def test_lower_is_better_gap_direction(self):
        result = audit_model_selection(
            [trial("t1", training_metric=0.10, development_metric=0.20, evaluation_metric=0.30)],
            metric_direction="lower",
        )
        self.assertAlmostEqual(result["training_development_overfit_gap"]["mean"], 0.10)
        self.assertAlmostEqual(result["development_evaluation_deterioration"]["mean"], 0.10)


if __name__ == "__main__":
    unittest.main()
