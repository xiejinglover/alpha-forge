from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CliEndToEndTests(unittest.TestCase):
    def run_ok(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([sys.executable, *args], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        return result

    def test_context_plan_training_and_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "data.csv"
            with dataset.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "asset", "label", "f1", "f2"])
                for day in range(1, 41):
                    for asset in range(5):
                        writer.writerow([f"2024-01-{day:02d}", f"A{asset}", asset - 2, asset, day])
            contract = root / "contract.json"
            contract.write_text(json.dumps({
                "columns": {"decision_time": "date", "label": "label", "features": ["f1", "f2"]},
                "task_type": "cross_sectional_ranking",
                "primary_metric": "ndcg_at_strategy_depth",
                "resource_budget": {"seeds": 2},
                "account_adapter": "will_be_confirmed",
            }), encoding="utf-8")
            context = root / "context"
            self.run_ok(str(SCRIPT_DIR / "inspect_factor_model_context.py"), "--dataset", str(dataset),
                        "--contract", str(contract), "--output-dir", str(context))

            adapter = root / "adapter.py"
            adapter.write_text(textwrap.dedent("""
                import argparse, csv, hashlib, json
                from pathlib import Path
                p=argparse.ArgumentParser(); p.add_argument('--trial'); p.add_argument('--output-dir'); a=p.parse_args()
                out=Path(a.output_dir)
                with (out/'predictions.csv').open('w', newline='') as h:
                    w=csv.writer(h); w.writerow(['decision_time','instrument_id','score']); w.writerow(['2025-01-02','A',0.5])
                (out/'metrics.json').write_text('{}')
                (out/'model_manifest.json').write_text('{}')
                digest=hashlib.sha256((out/'predictions.csv').read_bytes()).hexdigest()
                (out/'SUCCESS.json').write_text(json.dumps({'prediction_hash':digest}))
            """), encoding="utf-8")
            account = root / "account.py"
            account.write_text(textwrap.dedent("""
                import argparse, json
                from pathlib import Path
                p=argparse.ArgumentParser(); p.add_argument('--prediction-manifest'); p.add_argument('--strategy-spec'); p.add_argument('--output-dir'); a=p.parse_args()
                out=Path(a.output_dir); (out/'metrics.json').write_text('{}'); (out/'engine_manifest.json').write_text('{}'); (out/'SUCCESS.json').write_text('{}')
            """), encoding="utf-8")
            recommendation_hash = sha256(context / "recommended_spec.json")
            confirmed = root / "confirmed.json"
            confirmed.write_text(json.dumps({
                "user_confirmed": True, "recommended_spec_hash": recommendation_hash,
                "task_type": "cross_sectional_ranking", "confirmation_note": "test", "confirmed_at": "2026-01-01T00:00:00Z",
                "model": {"family": "dummy", "params": {}}, "preprocessing": {"missing": "zero"},
                "outer_folds": [{"fold_id": "2025", "train_start": "2022-01-01", "train_end": "2024-12-31", "validation_start": "2025-01-01", "validation_end": "2025-12-31"}],
                "seeds": [11, 13], "ensemble_sizes": [2], "strategy": {"selection": "project_defined"},
                "model_adapter": {"command": [sys.executable, str(adapter), "--trial", "{trial_json}", "--output-dir", "{trial_output_dir}"]},
                "account_adapter": {"command": [sys.executable, str(account), "--prediction-manifest", "{prediction_manifest}", "--strategy-spec", "{strategy_spec}", "--output-dir", "{account_output_dir}"]},
            }), encoding="utf-8")
            routes = root / "routes.jsonl"
            routes.write_text(json.dumps({"route_id": "raw", "screening_recipe_id": "r1", "method": "raw", "anchor": True}) + "\n", encoding="utf-8")
            plan = root / "plan"
            self.run_ok(str(SCRIPT_DIR / "build_factor_model_plan.py"), "--profile-dir", str(context),
                        "--confirmed-spec", str(confirmed), "--route-manifest", str(routes), "--output-dir", str(plan))
            run = root / "run"
            self.run_ok(str(SCRIPT_DIR / "run_factor_model_training.py"), "--plan-dir", str(plan), "--output-dir", str(run))
            success = json.loads((run / "SUCCESS.json").read_text())
            self.assertEqual(success["status"], "complete_account_validated")
            self.assertEqual(success["planned_trials"], 2)

    def test_plan_rejects_missing_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            profile.mkdir()
            (profile / "recommended_spec.json").write_text('{}')
            (profile / "data_profile.json").write_text('{}')
            spec = root / "spec.json"
            spec.write_text(json.dumps({"user_confirmed": False}))
            routes = root / "routes.jsonl"
            routes.write_text('{}\n')
            result = subprocess.run([
                sys.executable, str(SCRIPT_DIR / "build_factor_model_plan.py"), "--profile-dir", str(profile),
                "--confirmed-spec", str(spec), "--route-manifest", str(routes), "--output-dir", str(root / "out")
            ], text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
