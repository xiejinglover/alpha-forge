import io
import json
import sys
import tempfile
from contextlib import redirect_stderr
import unittest
from copy import deepcopy
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_audit_report import EXPECTED_MODULES, ManifestError, main, render_report, validate_manifest


def module(module_id):
    return {
        "module_id": module_id,
        "title": f"Title {module_id}",
        "role": "robustness test",
        "priority": "P1",
        "execution_mode": "REUSE_DIRECT",
        "execution_status": "COMPLETED",
        "blocked_reason_type": None,
        "verdict": "INCONCLUSIVE",
        "question": "Does the evidence remain stable?",
        "hypothesis": "Preregistered hypothesis",
        "repository_evidence": ["src/train.py: existing training entry"],
        "reused_code": ["src/train.py::train"],
        "commands": [{"command": "python -m project.train", "status": "success", "exit_code": 0, "output_summary": "completed", "artifacts": ["runs/test"]}],
        "changed_variables": {"seed": [1, 2]},
        "controlled_variables": {"folds": "frozen"},
        "cases": ["baseline", "case-1"],
        "data_scope": {"window": "2020-2024"},
        "thresholds": {},
        "metrics": [{"case_id": "baseline", "evidence_role": "development", "metric": "rank_ic", "value": 0.02, "unit": "ratio", "sample_count": 100}],
        "artifacts": ["runs/test/predictions.csv"],
        "facts": ["The existing training entry completed."],
        "interpretation": ["Evidence is insufficient without a threshold."],
        "limitations": ["No locked final OOS was read."],
        "skip_reason": None,
        "user_input_needed": [],
        "selection_impact": "No selection change.",
    }


def complete_manifest():
    phases = [
        "repository_discovery",
        "baseline_reproduction",
        "experiment_planning",
        "experiment_execution",
        "evidence_aggregation",
        "final_assessment",
    ]
    return {
        "report": {
            "title": "Audit report",
            "generated_at": "2026-08-05T00:00:00+08:00",
            "repository": {"path": "/repo", "remote": "https://example/repo", "commit": "abc", "branch": "main", "dirty": False},
            "strategy": {"name": "demo", "model": "tree", "market": "CN", "frequency": "daily", "label": "T+2", "decision_time": "close", "execution_time": "next-open"},
            "environment": {"python": "3.12", "dependencies": "locked", "hardware": "cpu"},
            "scope": list(EXPECTED_MODULES),
        },
        "overall": {
            "eligibility_verdict": "INCONCLUSIVE",
            "overfitting_verdict": "INCONCLUSIVE",
            "executive_conclusion": "Evidence remains incomplete.",
            "critical_findings": ["No structural violation observed."],
            "limitations": ["Thresholds are not preregistered."],
            "recommendations": ["Preregister thresholds."],
        },
        "repository_execution_map": [
            {
                "capability": "train",
                "paths": ["src/train.py"],
                "symbols": ["train"],
                "command": "python -m project.train",
                "config_paths": ["config.yaml"],
                "inputs": ["features"],
                "outputs": ["predictions"],
                "evidence": "covered by test_train.py",
            }
        ],
        "baseline": {
            "status": "COMPLETED",
            "command": "python -m project.train",
            "code_paths": ["src/train.py"],
            "config": "config.yaml",
            "data_window": "2020-2024",
            "artifacts": ["runs/baseline"],
            "metrics": {"rank_ic": 0.02},
            "historical_comparison": "matched",
            "conclusion": "Baseline reproduced with existing code.",
        },
        "phase_log": [
            {
                "step_id": f"STEP-{index:03d}",
                "phase": phase,
                "status": "COMPLETED",
                "objective": f"Complete {phase}",
                "action": f"Executed {phase}",
                "command": None,
                "inputs": ["repo"],
                "outputs": [phase],
                "result": "completed",
                "conclusion": f"{phase} complete",
            }
            for index, phase in enumerate(phases, start=1)
        ],
        "modules": [module(module_id) for module_id in EXPECTED_MODULES],
        "oos_access_ledger": [],
        "selection_record": {"dimensions": {}, "post_selection_events": [], "consumed_oos_ids": [], "missing_facts": []},
        "command_log": [],
        "file_changes": [],
        "errors": [],
    }


class BuildAuditReportTests(unittest.TestCase):
    def test_default_report_is_conclusion_first_and_hides_technical_detail(self):
        report = render_report(complete_manifest())
        self.assertIn("## 一、总体结论", report)
        self.assertIn("## 三、模块结论总览", report)
        self.assertIn("## 四、各模块详细分析", report)
        self.assertNotIn("src/train.py::train", report)
        self.assertNotIn("python -m project.train", report)
        self.assertNotIn("```json", report)
        for module_id in EXPECTED_MODULES:
            self.assertIn(f"### {module_id}", report)

    def test_technical_appendix_is_opt_in(self):
        report = render_report(complete_manifest(), include_technical_appendix=True)
        self.assertIn("## 技术证据附录", report)
        self.assertIn("src/train.py::train", report)
        self.assertIn("python -m project.train", report)
        self.assertIn("```json", report)

    def test_missing_module_is_rejected(self):
        manifest = complete_manifest()
        manifest["modules"] = manifest["modules"][:-1]
        with self.assertRaises(ManifestError):
            render_report(manifest)

    def test_skipped_module_requires_and_renders_reason(self):
        manifest = complete_manifest()
        skipped = deepcopy(manifest["modules"][1])
        skipped.update(
            {
                "execution_mode": "NO_SAFE_REUSE_PATH",
                "execution_status": "SKIPPED_UNAVAILABLE",
                "blocked_reason_type": "MISSING_REPOSITORY_CAPABILITY",
                "verdict": "BLOCKED",
                "reused_code": [],
                "commands": [],
                "facts": [],
                "interpretation": [],
                "skip_reason": "Existing repository exposes no configurable seed.",
            }
        )
        manifest["modules"][1] = skipped
        self.assertEqual(validate_manifest(manifest), [])
        report = render_report(manifest)
        self.assertIn("Existing repository exposes no configurable seed.", report)

    def test_user_decision_and_unavailable_capability_are_distinct(self):
        manifest = complete_manifest()
        waiting = manifest["modules"][0]
        waiting.update(
            {
                "execution_status": "NEEDS_USER_INPUT",
                "blocked_reason_type": "MISSING_USER_DECISION",
                "verdict": "BLOCKED",
                "reused_code": [],
                "commands": [],
                "facts": [],
                "interpretation": [],
                "skip_reason": "Multiple production configurations exist; the audit target is ambiguous.",
                "user_input_needed": ["Choose the production configuration."],
            }
        )
        self.assertEqual(validate_manifest(manifest), [])

        waiting["blocked_reason_type"] = "MISSING_REPOSITORY_CAPABILITY"
        errors = validate_manifest(manifest)
        self.assertTrue(any("user-decision or authorization" in error for error in errors))

    def test_discovery_only_report_keeps_empty_execution_fields(self):
        manifest = complete_manifest()
        for item in manifest["modules"]:
            item.update(
                {
                    "execution_status": "NEEDS_USER_INPUT",
                    "blocked_reason_type": "EXECUTION_NOT_AUTHORIZED",
                    "verdict": "BLOCKED",
                    "reused_code": [],
                    "commands": [],
                    "cases": [],
                    "metrics": [],
                    "artifacts": [],
                    "facts": [],
                    "interpretation": [],
                    "skip_reason": "Repository inspection completed; experiment execution was not authorized.",
                    "user_input_needed": ["Authorize the existing repository command."],
                }
            )
        report = render_report(manifest)
        self.assertIn("Repository inspection completed; experiment execution was not authorized.", report)

    def test_phase_status_is_required(self):
        manifest = complete_manifest()
        del manifest["phase_log"][0]["status"]
        errors = validate_manifest(manifest)
        self.assertTrue(any("missing fields: status" in error for error in errors))

    def test_executed_module_without_reused_code_is_rejected(self):
        manifest = complete_manifest()
        manifest["modules"][0]["reused_code"] = []
        errors = validate_manifest(manifest)
        self.assertTrue(any("reused_code is empty" in error for error in errors))

    def test_cli_writes_report_and_refuses_silent_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "manifest.json"
            output_path = Path(directory) / "report.md"
            input_path.write_text(json.dumps(complete_manifest()), encoding="utf-8")
            args = ["--input", str(input_path), "--output", str(output_path)]
            self.assertEqual(main(args), 0)
            first_report = output_path.read_text(encoding="utf-8")
            self.assertIn("### ML-005", first_report)
            self.assertNotIn("技术证据附录", first_report)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual(main([*args, "--force"]), 0)
            self.assertEqual(output_path.read_text(encoding="utf-8"), first_report)

            self.assertEqual(main([*args, "--force", "--include-technical-appendix"]), 0)
            self.assertIn("技术证据附录", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
