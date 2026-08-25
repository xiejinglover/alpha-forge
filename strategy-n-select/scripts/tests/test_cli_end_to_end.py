from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
RUNNER = SCRIPT_DIR / "run_n_select.py"
SCORER = SCRIPT_DIR / "compute_candidate_scores.py"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class CliEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.study = self.root / "study.json"
        self.study.write_text(
            json.dumps(
                {
                    "development": {"start": "2024-01-01", "end": "2024-12-31"},
                    "holdout": {
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "consumed_for_selection": False,
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_command(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, *args],
            cwd=SCRIPT_DIR,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, expected, msg=result.stderr or result.stdout)
        return result

    def test_score_select_vote_and_hash_guard(self) -> None:
        returns = self.root / "returns.csv"
        write_csv(
            returns,
            ["candidate_id", "date", "net_return"],
            [
                {"candidate_id": candidate, "date": day, "net_return": value}
                for candidate, values in {
                    "S1": [0.01, -0.002, 0.012],
                    "S2": [0.003, -0.001, 0.004],
                    "S3": [-0.002, 0.006, 0.002],
                }.items()
                for day, value in zip(("2024-01-02", "2024-02-02", "2024-03-02"), values)
            ],
        )
        scores = self.root / "scores.csv"
        self.run_command(
            str(SCORER),
            "--returns",
            str(returns),
            "--study-config",
            str(self.study),
            "--output",
            str(scores),
        )
        selection_dir = self.root / "selection"
        self.run_command(
            str(RUNNER),
            "select",
            "--scores",
            str(scores),
            "--study-config",
            str(self.study),
            "--metric",
            "sharpe",
            "--direction",
            "max",
            "--n-values",
            "1,3",
            "--matches",
            "5",
            "--output-dir",
            str(selection_dir),
        )
        manifest = json.loads((selection_dir / "selection_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["defaults"]["n_values"], [1, 3, 5, 10, 20, 100, 300, 1000])
        self.assertTrue(manifest["overrides"]["n_values"])
        self.assertTrue(manifest["overrides"]["matches"])
        self.assertFalse(manifest["overrides"]["seed"])

        rejected = self.run_command(
            str(RUNNER),
            "vote",
            "--decorrelation-manifest",
            str(selection_dir / "selection_manifest.json"),
            "--signals",
            str(scores),
            "--output-dir",
            str(self.root / "rejected-raw-selection"),
            expected=2,
        )
        self.assertIn("Unsupported or unfrozen decorrelation manifest", rejected.stderr)

        with (selection_dir / "members.csv").open(encoding="utf-8") as handle:
            members = list(csv.DictReader(handle))
        member_ids = sorted({row["candidate_id"] for row in members})
        development_signals = self.root / "development_signals.csv"
        development_assets = {
            candidate_id: (f"{candidate_id}-A", f"{candidate_id}-B")
            for candidate_id in member_ids
        }
        write_csv(
            development_signals,
            ["rebalance_date", "candidate_id", "asset_id"],
            [
                {
                    "rebalance_date": day,
                    "candidate_id": candidate_id,
                    "asset_id": asset_id,
                }
                for day in ("2024-04-01", "2024-07-01")
                for candidate_id in member_ids
                for asset_id in development_assets[candidate_id]
            ],
        )
        decorrelation_dir = self.root / "decorrelation"
        self.run_command(
            str(RUNNER),
            "decorrelate",
            "--selection-manifest",
            str(selection_dir / "selection_manifest.json"),
            "--development-signals",
            str(development_signals),
            "--threshold",
            "0.6",
            "--output-dir",
            str(decorrelation_dir),
        )
        with (decorrelation_dir / "decorrelated_members.csv").open(encoding="utf-8") as handle:
            decorrelated_members = list(csv.DictReader(handle))
        self.assertEqual(
            {row["candidate_id"] for row in decorrelated_members},
            set(member_ids),
        )

        signals = self.root / "signals.csv"
        write_csv(
            signals,
            ["rebalance_date", "candidate_id", "asset_id"],
            [
                {
                    "rebalance_date": day,
                    "candidate_id": candidate_id,
                    "asset_id": asset_id,
                }
                for day in ("2025-01-06", "2025-02-03")
                for candidate_id in member_ids
                for asset_id in (("A", "C") if candidate_id == "S1" else ("B",))
            ],
        )
        portfolio_dir = self.root / "portfolio"
        self.run_command(
            str(RUNNER),
            "vote",
            "--decorrelation-manifest",
            str(decorrelation_dir / "decorrelation_manifest.json"),
            "--signals",
            str(signals),
            "--top-k",
            "1,3,5",
            "--output-dir",
            str(portfolio_dir),
        )
        with (portfolio_dir / "target_weights.csv").open(encoding="utf-8") as handle:
            targets = list(csv.DictReader(handle))
        self.assertTrue(targets)
        self.assertEqual({row["member_vote_mode"] for row in targets}, {"slot_weighted", "unique_equal"})
        self.assertEqual({row["selection_label"] for row in targets}, {"all", "top_1", "top_3", "top_5"})
        with (portfolio_dir / "member_votes.csv").open(encoding="utf-8") as handle:
            member_votes = list(csv.DictReader(handle))
        s1_rows = [row for row in member_votes if row["candidate_id"] == "S1"]
        if s1_rows:
            self.assertTrue(all(row["member_asset_count"] == "2" for row in s1_rows))

        with (decorrelation_dir / "decorrelated_members.csv").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        failed_dir = self.root / "should-not-exist"
        result = self.run_command(
            str(RUNNER),
            "vote",
            "--decorrelation-manifest",
            str(decorrelation_dir / "decorrelation_manifest.json"),
            "--signals",
            str(signals),
            "--output-dir",
            str(failed_dir),
            expected=2,
        )
        self.assertIn("Frozen decorrelation artifact changed", result.stderr)


if __name__ == "__main__":
    unittest.main()
