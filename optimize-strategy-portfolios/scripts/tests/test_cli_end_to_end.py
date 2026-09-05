from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
RUNNER = SCRIPT_DIR / "run_portfolio_optimization.py"
REPORTER = SCRIPT_DIR / "build_portfolio_report.py"


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.est_days = [date(2024, 1, 2) + timedelta(days=i) for i in range(30)]
        self.eval_days = [date(2025, 1, 2) + timedelta(days=i) for i in range(30)]
        self.benchmark_values = [(-1 if i % 3 == 0 else 1) * (0.003 + i * 0.0002) for i in range(60)]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_command(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, *args], cwd=SCRIPT_DIR, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, expected, msg=result.stderr or result.stdout)
        return result

    def test_full_pipeline_filters_negative_alpha_and_freezes_equal_sleeves(self) -> None:
        study = self.root / "study.json"
        study.write_text(json.dumps({
            "family_id": "family", "annualization": 252, "quality_top_k": 100,
            "quality_method": "ema20_robust_specific_quality_v1",
            "n_values": [2], "hac_lags": 1,
            "minimum_positive_residual_periods": 4,
            "deployments": [{
                "deployment_id": "2025",
                "estimation": {"start": "2024-01-01", "end": "2024-12-31"},
                "evaluation": {"start": "2025-01-01", "end": "2025-12-31"},
                "benchmark_id": "family", "evidence_role": "consumed_research",
                "consumed_for_selection": True,
                "evaluation_dataset_id": "eval-2025",
                "evaluation_accessed_at": "2026-01-01T00:00:00",
            }],
        }), encoding="utf-8")
        all_days = self.est_days + self.eval_days
        benchmark = self.root / "benchmark.csv"
        write_csv(benchmark, ["benchmark_id", "date", "net_return"], [
            {"benchmark_id": "family", "date": day.isoformat(), "net_return": value}
            for day, value in zip(all_days, self.benchmark_values)
        ])
        candidates = self.root / "candidates.csv"
        definitions = {
            "A": (0.0015, 0.2), "B": (0.0012, 0.4), "C": (0.0010, 0.8), "D": (-0.001, 0.02),
        }
        definitions.update({f"X{index:03d}": (0.0002, 0.9 + index / 10000) for index in range(100)})
        candidate_rows = []
        for candidate, (alpha, beta) in definitions.items():
            for index, (day, benchmark_return) in enumerate(zip(all_days, self.benchmark_values)):
                period_alpha = alpha if index < len(self.est_days) else (-0.001 if candidate in {"A", "B"} else alpha)
                candidate_rows.append({
                    "candidate_id": candidate, "date": day.isoformat(),
                    "net_return": period_alpha + beta * benchmark_return + (index % 2) * 0.00001,
                })
        write_csv(candidates, ["candidate_id", "date", "net_return"], candidate_rows)
        candidate_hash = hashlib.sha256(candidates.read_bytes()).hexdigest()
        diagnostics = self.root / "diagnostics.csv"
        write_csv(diagnostics, [
            "deployment_id", "family_id", "candidate_id", "eligible", "robust_quality_score",
            "trading_activity_ok", "coverage_complete", "positive_residual_periods",
            "residual_periods_total", "estimation_start", "estimation_end", "as_of",
            "rule_id", "source_artifact_path", "source_artifact_sha256",
        ], [
            {"deployment_id": "2025", "family_id": "family", "candidate_id": candidate, "eligible": "true",
             "robust_quality_score": score, "trading_activity_ok": "true",
             "coverage_complete": "true", "positive_residual_periods": 6,
             "residual_periods_total": 6, "estimation_start": "2024-01-01",
             "estimation_end": "2024-12-31", "as_of": "2024-12-31",
             "rule_id": "quality-v1", "source_artifact_path": str(candidates),
             "source_artifact_sha256": candidate_hash}
            for candidate, score in {
                **{"A": 102, "B": 103, "C": 104, "D": 1000},
                **{f"X{index:03d}": index for index in range(100)},
            }.items()
        ])
        controls = self.root / "controls.csv"
        write_csv(controls, ["date", "factor_id", "return"], [
            {"date": day.isoformat(), "factor_id": "style", "return": ((index % 5) - 2) * 0.0003}
            for index, day in enumerate(all_days)
        ])
        clusters = self.root / "clusters.csv"
        write_csv(clusters, [
            "deployment_id", "candidate_id", "cluster_id", "estimation_start", "estimation_end",
            "as_of", "rule_id", "source_artifact_path", "source_artifact_sha256",
        ], [
            {"deployment_id": "2025", "candidate_id": candidate, "cluster_id": cluster,
             "estimation_start": "2024-01-01", "estimation_end": "2024-12-31",
             "as_of": "2024-12-31", "rule_id": "clusters-v1",
             "source_artifact_path": str(candidates), "source_artifact_sha256": candidate_hash}
            for candidate, cluster in {"A": "1", "B": "2", "C": "1", "D": "2"}.items()
        ])
        signals = self.root / "signals.csv"
        write_csv(signals, ["date", "candidate_id", "asset_id"], [
            {"date": day.isoformat(), "candidate_id": candidate, "asset_id": asset}
            for day in self.est_days
            for candidate, asset in {"A": "X", "B": "Y", "C": "X", "D": "Z"}.items()
        ])
        output = self.root / "output"
        self.run_command(
            str(RUNNER), "--study", str(study), "--candidate-returns", str(candidates),
            "--benchmark-returns", str(benchmark), "--diagnostics", str(diagnostics),
            "--controls", str(controls),
            "--output-dir", str(output),
        )
        with (output / "candidate_risk_metrics.csv").open(encoding="utf-8") as handle:
            risk = {row["candidate_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(risk["D"]["eligible"], "False")
        self.assertIn("nonpositive_residual_alpha", risk["D"]["exclusion_reasons"])
        with (output / "portfolio_members.csv").open(encoding="utf-8") as handle:
            members = list(csv.DictReader(handle))
        low_beta = [row for row in members if row["scheme"] == "LOW_BETA_EQ"]
        self.assertEqual({row["candidate_id"] for row in low_beta}, {"A", "B"})
        self.assertTrue(all(abs(float(row["weight"]) - 0.5) < 1e-12 for row in low_beta))
        self.assertEqual({row["scheme"] for row in members}, {"LOW_BETA_EQ"})
        with (output / "pairwise_diagnostics.csv").open(encoding="utf-8") as handle:
            pairs = list(csv.DictReader(handle))
        self.assertTrue(all(row["signal_jaccard"] == "" for row in pairs))
        with (output / "portfolio_metrics.csv").open(encoding="utf-8") as handle:
            metrics = list(csv.DictReader(handle))
        low_eval = next(row for row in metrics if row["scheme"] == "LOW_BETA_EQ" and row["period"] == "evaluation")
        self.assertLess(float(low_eval["residual_alpha"]), 0)
        self.assertTrue(any(row["deployment_id"] == "ALL_ROLLING" for row in metrics))
        report = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn("diagnostic_only", report)
        self.assertIn("低 Beta 只在质量保护后", report)

        independent_data = json.loads(study.read_text(encoding="utf-8"))
        independent_deployment = independent_data["deployments"][0]
        selection_spec = self.root / "selection-spec.json"
        selection_spec.write_text(json.dumps({
            "family_id": "family", "frozen_at": "2024-12-31T16:00:00",
            "datasets": {"eval-2025": {
                "estimation_start": "2024-01-01", "estimation_end": "2024-12-31",
                "evaluation_start": "2025-01-01", "evaluation_end": "2025-12-31",
                "benchmark_id": "family",
            }},
        }, sort_keys=True), encoding="utf-8")
        selection_hash = hashlib.sha256(selection_spec.read_bytes()).hexdigest()
        prior_payload = {
            "dataset_id": "eval-2025", "access_order": 0,
            "event_time": "2024-12-31T16:00:00", "purpose": "freeze",
            "used_for_selection": False, "status": "prepared_not_accessed",
            "selection_spec_hash": selection_hash, "previous_event_hash": "",
        }
        prior_event_hash = hashlib.sha256(json.dumps(
            prior_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        independent_deployment.update({
            "evidence_role": "independent_holdout", "consumed_for_selection": False,
            "selection_frozen_at": "2024-12-31T16:00:00",
            "evaluation_accessed_at": "2026-01-01T00:00:00",
            "selection_spec_hash": selection_hash,
        })
        independent_study = self.root / "independent-study.json"
        independent_study.write_text(json.dumps(independent_data), encoding="utf-8")
        missing_ledger = self.run_command(
            str(RUNNER), "--study", str(independent_study),
            "--candidate-returns", str(candidates), "--benchmark-returns", str(benchmark),
            "--diagnostics", str(diagnostics), "--controls", str(controls),
            "--selection-spec", str(selection_spec),
            "--output-dir", str(self.root / "missing-ledger"), expected=2,
        )
        self.assertIn("requires prior locked ledger evidence", missing_ledger.stderr)
        prior_ledger = self.root / "prior-ledger.csv"
        write_csv(prior_ledger, [
            "dataset_id", "access_order", "event_time", "purpose", "used_for_selection",
            "status", "selection_spec_hash", "previous_event_hash", "event_hash",
        ], [{
            "dataset_id": "eval-2025", "access_order": 0,
            "event_time": "2024-12-31T16:00:00", "purpose": "freeze",
            "used_for_selection": "false", "status": "prepared_not_accessed",
            "selection_spec_hash": selection_hash, "previous_event_hash": "",
            "event_hash": prior_event_hash,
        }])
        independent_output = self.root / "independent-output"
        self.run_command(
            str(RUNNER), "--study", str(independent_study),
            "--candidate-returns", str(candidates), "--benchmark-returns", str(benchmark),
            "--diagnostics", str(diagnostics), "--controls", str(controls),
            "--prior-oos-ledger", str(prior_ledger), "--selection-spec", str(selection_spec),
            "--output-dir", str(independent_output),
        )
        self.assertIn(
            "research_candidate_pending_forward",
            (independent_output / "report.md").read_text(encoding="utf-8"),
        )
        forward_rules = {
            "minimum_return_retention": -999.0, "minimum_sharpe_delta": -999.0,
            "maximum_drawdown_worsening": 999.0,
            "minimum_ordinary_beta_reduction": -999.0,
            "minimum_downside_beta_reduction": -999.0,
            "minimum_tail10_beta_reduction": -999.0,
            "minimum_residual_alpha": -999.0, "minimum_config_pass_rate": 0.0,
        }
        forward_spec_value = json.loads(selection_spec.read_text(encoding="utf-8"))
        forward_spec_value["decision_rules"] = forward_rules
        forward_spec = self.root / "forward-selection-spec.json"
        forward_spec.write_text(json.dumps(forward_spec_value, sort_keys=True), encoding="utf-8")
        forward_spec_hash = hashlib.sha256(forward_spec.read_bytes()).hexdigest()
        forward_payload = {**prior_payload, "selection_spec_hash": forward_spec_hash}
        forward_event_hash = hashlib.sha256(json.dumps(
            forward_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        forward_ledger = self.root / "forward-ledger.csv"
        write_csv(forward_ledger, [
            "dataset_id", "access_order", "event_time", "purpose", "used_for_selection",
            "status", "selection_spec_hash", "previous_event_hash", "event_hash",
        ], [{**forward_payload, "used_for_selection": "false", "event_hash": forward_event_hash}])
        forward_data = json.loads(json.dumps(independent_data))
        forward_data["decision_rules"] = forward_rules
        forward_data["deployments"][0]["evidence_role"] = "forward_monitoring"
        forward_data["deployments"][0]["selection_spec_hash"] = forward_spec_hash
        forward_study = self.root / "forward-study.json"
        forward_study.write_text(json.dumps(forward_data), encoding="utf-8")
        forward_output = self.root / "forward-output"
        self.run_command(
            str(RUNNER), "--study", str(forward_study),
            "--candidate-returns", str(candidates), "--benchmark-returns", str(benchmark),
            "--diagnostics", str(diagnostics), "--controls", str(controls),
            "--prior-oos-ledger", str(forward_ledger), "--selection-spec", str(forward_spec),
            "--output-dir", str(forward_output),
        )
        self.assertIn(
            "research_candidate_pending_forward",
            (forward_output / "report.md").read_text(encoding="utf-8"),
        )

        rejected = self.run_command(
            str(RUNNER), "--study", str(study), "--candidate-returns", str(candidates),
            "--benchmark-returns", str(benchmark), "--diagnostics", str(diagnostics),
            "--controls", str(controls),
            "--output-dir", str(output), expected=2,
        )
        self.assertIn("not empty", rejected.stderr)

        report_path = output / "report.md"
        original_report = report_path.read_text(encoding="utf-8")
        report_path.write_text(original_report + "tampered\n", encoding="utf-8")
        report_tamper = self.run_command(
            str(REPORTER), "--manifest", str(output / "frozen_spec.json"),
            "--output", str(self.root / "report-tamper.md"), expected=2,
        )
        self.assertIn("frozen artifact changed", report_tamper.stderr)
        report_path.write_text(original_report, encoding="utf-8")
        with (output / "portfolio_members.csv").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        rebuilt = self.run_command(
            str(REPORTER), "--manifest", str(output / "frozen_spec.json"),
            "--output", str(self.root / "rebuilt.md"), expected=2,
        )
        self.assertIn("frozen artifact changed", rebuilt.stderr)

    def test_missing_diagnostic_candidate_is_rejected(self) -> None:
        study = self.root / "study.json"
        study.write_text(json.dumps({
            "family_id": "family", "annualization": 252, "quality_top_k": 100,
            "quality_method": "ema20_robust_specific_quality_v1",
            "n_values": [1], "hac_lags": 0,
            "deployments": [{
                "deployment_id": "2025",
                "estimation": {"start": "2024-01-01", "end": "2024-12-31"},
                "evaluation": {"start": "2025-01-01", "end": "2025-12-31"},
                "benchmark_id": "family", "evidence_role": "consumed_research",
                "consumed_for_selection": True,
                "evaluation_dataset_id": "eval-2025",
                "evaluation_accessed_at": "2026-01-01T00:00:00",
            }],
        }), encoding="utf-8")
        all_days = self.est_days + self.eval_days
        benchmark = self.root / "benchmark.csv"
        write_csv(benchmark, ["benchmark_id", "date", "net_return"], [
            {"benchmark_id": "family", "date": day.isoformat(), "net_return": value}
            for day, value in zip(all_days, self.benchmark_values)
        ])
        candidates = self.root / "candidates.csv"
        write_csv(candidates, ["candidate_id", "date", "net_return"], [
            {"candidate_id": candidate, "date": day.isoformat(), "net_return": 0.001 + beta * value}
            for candidate, beta in (("A", .2), ("B", .3))
            for day, value in zip(all_days, self.benchmark_values)
        ])
        candidate_hash = hashlib.sha256(candidates.read_bytes()).hexdigest()
        diagnostics = self.root / "diagnostics.csv"
        write_csv(diagnostics, [
            "deployment_id", "family_id", "candidate_id", "eligible", "robust_quality_score",
            "trading_activity_ok", "coverage_complete", "positive_residual_periods",
            "residual_periods_total", "estimation_start", "estimation_end", "as_of",
            "rule_id", "source_artifact_path", "source_artifact_sha256",
        ], [{
            "deployment_id": "2025", "family_id": "family", "candidate_id": "A", "eligible": "true",
            "robust_quality_score": 1, "trading_activity_ok": "true", "coverage_complete": "true",
            "positive_residual_periods": 6, "residual_periods_total": 6,
            "estimation_start": "2024-01-01", "estimation_end": "2024-12-31",
            "as_of": "2024-12-31", "rule_id": "quality-v1",
            "source_artifact_path": str(candidates), "source_artifact_sha256": candidate_hash,
        }])
        result = self.run_command(
            str(RUNNER), "--study", str(study), "--candidate-returns", str(candidates),
            "--benchmark-returns", str(benchmark), "--diagnostics", str(diagnostics),
            "--output-dir", str(self.root / "output"), expected=2,
        )
        self.assertIn("diagnostic denominator mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
