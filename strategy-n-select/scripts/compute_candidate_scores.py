#!/usr/bin/env python3
"""Compute built-in development-period candidate scores from net returns."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from n_select_core import ContractError, load_study_config, parse_iso_date, write_csv


SUPPORTED_METRICS = ("sharpe", "calmar", "positive_month_rate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--returns", required=True, type=Path, help="CSV: candidate_id,date,net_return")
    parser.add_argument("--study-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metrics", default=",".join(SUPPORTED_METRICS))
    parser.add_argument("--annualization", type=int, default=252)
    parser.add_argument("--risk-free-periodic", type=float, default=0.0)
    return parser.parse_args()


def load_returns(path: Path, development_start, development_end) -> dict[str, list[tuple[object, float]]]:
    result: dict[str, list[tuple[object, float]]] = defaultdict(list)
    seen: set[tuple[str, object]] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"candidate_id", "date", "net_return"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ContractError(f"{path} is missing required columns: {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            candidate_id = (row.get("candidate_id") or "").strip()
            if not candidate_id:
                raise ContractError(f"{path}:{line_number} has blank candidate_id")
            observation_date = parse_iso_date((row.get("date") or "").strip(), f"{path}:{line_number} date")
            if not development_start <= observation_date <= development_end:
                continue
            key = (candidate_id, observation_date)
            if key in seen:
                raise ContractError(f"{path}:{line_number} duplicates candidate_id + date")
            seen.add(key)
            try:
                value = float(row["net_return"])
            except (TypeError, ValueError) as exc:
                raise ContractError(f"{path}:{line_number} has invalid net_return") from exc
            if not math.isfinite(value):
                raise ContractError(f"{path}:{line_number} has non-finite net_return")
            if value <= -1:
                raise ContractError(f"{path}:{line_number} has net_return <= -1, invalid for compounding")
            result[candidate_id].append((observation_date, value))
    if not result:
        raise ContractError(f"No returns inside the development period in {path}")
    for observations in result.values():
        observations.sort(key=lambda item: item[0])
    return dict(result)


def score_sharpe(returns: list[float], annualization: int, risk_free_periodic: float) -> float:
    if len(returns) < 2:
        return math.nan
    standard_deviation = statistics.stdev(returns)
    if standard_deviation == 0:
        return math.nan
    return (statistics.mean(returns) - risk_free_periodic) / standard_deviation * math.sqrt(annualization)


def score_calmar(returns: list[float], annualization: int) -> float:
    if len(returns) < 2:
        return math.nan
    equity = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak)
    if maximum_drawdown == 0:
        return math.nan
    annualized_return = equity ** (annualization / len(returns)) - 1.0
    return annualized_return / maximum_drawdown


def score_positive_month_rate(observations: list[tuple[object, float]]) -> float:
    monthly: dict[tuple[int, int], float] = defaultdict(lambda: 1.0)
    for observation_date, value in observations:
        monthly[(observation_date.year, observation_date.month)] *= 1.0 + value
    if not monthly:
        return math.nan
    positive = sum(1 for compounded in monthly.values() if compounded - 1.0 > 0)
    return positive / len(monthly)


def main() -> int:
    args = parse_args()
    try:
        if args.annualization <= 0:
            raise ContractError("annualization must be positive")
        metrics = [metric.strip() for metric in args.metrics.split(",") if metric.strip()]
        unknown = sorted(set(metrics) - set(SUPPORTED_METRICS))
        if not metrics or unknown:
            raise ContractError(f"metrics must be selected from {SUPPORTED_METRICS}; unknown={unknown}")
        study = load_study_config(args.study_config)
        development_start = parse_iso_date(study["development"]["start"], "development.start")
        development_end = parse_iso_date(study["development"]["end"], "development.end")
        returns_by_candidate = load_returns(args.returns, development_start, development_end)
        rows = []
        for candidate_id in sorted(returns_by_candidate):
            observations = returns_by_candidate[candidate_id]
            values = [value for _, value in observations]
            for metric in metrics:
                if metric == "sharpe":
                    score = score_sharpe(values, args.annualization, args.risk_free_periodic)
                elif metric == "calmar":
                    score = score_calmar(values, args.annualization)
                else:
                    score = score_positive_month_rate(observations)
                rows.append(
                    {
                        "candidate_id": candidate_id,
                        "metric": metric,
                        "score": format(score, ".17g") if math.isfinite(score) else "",
                    }
                )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise ContractError(f"Refusing to overwrite existing output: {args.output}")
        write_csv(args.output, ["candidate_id", "metric", "score"], rows)
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
