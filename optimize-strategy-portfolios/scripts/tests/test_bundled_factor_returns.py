from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from validate_bundled_factor_returns import (
    DEFAULT_ASSET,
    DEFAULT_MANIFEST,
    ValidationError,
    validate,
)
from run_portfolio_optimization import DEFAULT_CONTROL_RETURNS, build_parser


class BundledFactorReturnTests(unittest.TestCase):
    def test_bundled_snapshot_matches_manifest(self) -> None:
        result = validate(DEFAULT_ASSET, DEFAULT_MANIFEST)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["rows"], 4536)
        self.assertEqual(result["factors"], 9)
        self.assertEqual(result["end"], "2026-08-31")

    def test_runner_uses_bundled_snapshot_by_default(self) -> None:
        args = build_parser().parse_args([
            "--study", "study.json",
            "--candidate-returns", "candidates.csv",
            "--benchmark-returns", "benchmark.csv",
            "--diagnostics", "diagnostics.csv",
            "--output-dir", "output",
        ])
        self.assertEqual(args.controls, DEFAULT_CONTROL_RETURNS)

    def test_tampered_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asset = root / "controls.csv"
            asset.write_text("trade_date,beta\n2024-01-02,0.1\n", encoding="utf-8")
            manifest = root / "controls.manifest.json"
            manifest.write_text(json.dumps({
                "asset_file": asset.name,
                "columns": ["trade_date", "beta"],
                "snapshot": {
                    "size_bytes": asset.stat().st_size,
                    "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    "row_count": 1,
                    "start_trade_date": "2024-01-02",
                    "end_trade_date": "2024-01-02",
                    "as_of_trade_date": "2024-01-02",
                },
            }), encoding="utf-8")
            with asset.open("a", encoding="utf-8") as handle:
                handle.write("2024-01-03,0.2\n")
            with self.assertRaisesRegex(ValidationError, "size"):
                validate(asset, manifest)


if __name__ == "__main__":
    unittest.main()
