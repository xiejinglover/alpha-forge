import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_temporal_integrity import audit_oos_ledger, audit_samples, audit_temporal_integrity


def valid_sample(**overrides):
    row = {
        "sample_id": "s1",
        "fold_id": "f1",
        "case_id": "baseline",
        "feature_cutoff": "5",
        "decision_at": "5",
        "label_end": "7",
        "fit_as_of": "7",
        "train_end": "4",
        "validation_start": "8",
        "label_horizon": "2",
        "purge": "2",
        "embargo": "2",
        "in_training": "false",
        "prediction_role": "development_oof",
        "prediction_owner": "model-f1",
    }
    row.update(overrides)
    return row


def ledger_event(**overrides):
    row = {
        "dataset_id": "final-2025",
        "access_order": "1",
        "purpose": "final_confirmation",
        "used_for_selection": "false",
        "status": "locked",
    }
    row.update(overrides)
    return row


class TemporalIntegrityTests(unittest.TestCase):
    def test_valid_time_chain_and_single_confirmation_pass(self):
        result = audit_temporal_integrity([valid_sample()], [ledger_event()])
        self.assertEqual(result["eligibility_verdict"], "PASSED")
        self.assertEqual(result["issues"], [])

    def test_future_data_short_purge_and_training_contamination_fail(self):
        result = audit_samples(
            [
                valid_sample(
                    feature_cutoff="6",
                    decision_at="5",
                    label_end="9",
                    fit_as_of="7",
                    purge="1",
                    embargo="1",
                    in_training="true",
                )
            ]
        )
        codes = {issue["code"] for issue in result["issues"]}
        self.assertTrue(
            {"FEATURE_AFTER_DECISION", "LABEL_AFTER_FIT", "PURGE_TOO_SHORT", "EMBARGO_TOO_SHORT", "OOF_TRAINING_CONTAMINATION"}
            <= codes
        )

    def test_multiple_prediction_owners_fail(self):
        result = audit_samples(
            [valid_sample(prediction_owner="owner-a"), valid_sample(prediction_owner="owner-b")]
        )
        self.assertIn("MULTIPLE_PREDICTION_OWNERS", {issue["code"] for issue in result["issues"]})

    def test_selection_downgrades_oos_and_blocks_later_confirmation(self):
        result = audit_oos_ledger(
            [
                ledger_event(purpose="tuning", used_for_selection="true"),
                ledger_event(access_order="2", purpose="final_confirmation"),
            ]
        )
        self.assertTrue(all(row["effective_status"] == "consumed_for_selection" for row in result["normalized_ledger"]))
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("CONFIRMATION_AFTER_SELECTION", codes)

    def test_selection_after_confirmation_invalidates_final_use(self):
        result = audit_oos_ledger(
            [ledger_event(), ledger_event(access_order="2", purpose="manual_readd", used_for_selection="true")]
        )
        self.assertIn("POST_CONFIRMATION_SELECTION", {issue["code"] for issue in result["issues"]})


if __name__ == "__main__":
    unittest.main()
