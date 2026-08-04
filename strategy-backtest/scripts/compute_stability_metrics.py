#!/usr/bin/env python3
"""Compute deterministic stability metrics for paired ML predictions.

Input CSV columns: date, asset, case_id, seed, score, target.  Empty or
non-finite score/target values are treated as missing.  A variant is the pair
(case_id, seed).  The module uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PREDICTION_FIELDS = ("date", "asset", "case_id", "seed", "score", "target")


def _finite_float(value: Any, field: str = "value", location: str = "record") -> float | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}: {field} must be numeric or empty") from exc
    return number if math.isfinite(number) else None


def quantile(values: Sequence[float], probability: float) -> float | None:
    """Return a type-7 linearly interpolated sample quantile."""

    if not values:
        return None
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def population_std(values: Sequence[float]) -> float | None:
    if not values:
        return None
    center = mean(values)
    assert center is not None
    return math.sqrt(sum((value - center) ** 2 for value in values) / len(values))


def pearson(x_values: Sequence[float], y_values: Sequence[float]) -> float | None:
    """Return Pearson correlation, or None for insufficient/constant data."""

    if len(x_values) != len(y_values) or len(x_values) < 2:
        return None
    x_mean = mean(x_values)
    y_mean = mean(y_values)
    assert x_mean is not None and y_mean is not None
    x_centered = [value - x_mean for value in x_values]
    y_centered = [value - y_mean for value in y_values]
    denominator = math.sqrt(
        sum(value * value for value in x_centered)
        * sum(value * value for value in y_centered)
    )
    if denominator == 0.0:
        return None
    return sum(x * y for x, y in zip(x_centered, y_centered)) / denominator


def average_ranks(values: Sequence[float]) -> list[float]:
    """Return one-based average ranks with deterministic tie handling."""

    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(indexed):
        end = start + 1
        while end < len(indexed) and indexed[end][1] == indexed[start][1]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        for position in range(start, end):
            ranks[indexed[position][0]] = average_rank
        start = end
    return ranks


def spearman(x_values: Sequence[float], y_values: Sequence[float]) -> float | None:
    if len(x_values) != len(y_values) or len(x_values) < 2:
        return None
    return pearson(average_ranks(x_values), average_ranks(y_values))


def top_k_assets(scores: Mapping[str, float], top_k: int) -> set[str]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {asset for asset, _ in ordered[:top_k]}


def jaccard(left: set[str], right: set[str]) -> float | None:
    union = left | right
    return len(left & right) / len(union) if union else None


def summarize(values: Iterable[float | None], worst_quantile: float, direction: str = "higher") -> dict[str, Any]:
    finite = [value for value in values if value is not None and math.isfinite(value)]
    worst_probability = worst_quantile if direction == "higher" else 1.0 - worst_quantile
    q25 = quantile(finite, 0.25)
    q75 = quantile(finite, 0.75)
    return {
        "count": len(finite),
        "mean": mean(finite),
        "median": quantile(finite, 0.5),
        "iqr": None if q25 is None or q75 is None else q75 - q25,
        "worst_quantile": quantile(finite, worst_probability),
        "positive_sign_rate": (sum(value > 0 for value in finite) / len(finite)) if finite else None,
    }


def read_predictions(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header")
        missing = [field for field in PREDICTION_FIELDS if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
        records: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for number, row in enumerate(reader, start=2):
            date = str(row["date"]).strip()
            asset = str(row["asset"]).strip()
            case_id = str(row["case_id"]).strip()
            seed = str(row["seed"]).strip()
            if not date or not asset or not case_id:
                raise ValueError(f"{path}:{number}: date, asset, and case_id must be non-empty")
            key = (case_id, seed, date, asset)
            if key in seen:
                raise ValueError(f"{path}:{number}: duplicate prediction key {key}")
            seen.add(key)
            records.append(
                {
                    "date": date,
                    "asset": asset,
                    "case_id": case_id,
                    "seed": seed,
                    "score": _finite_float(row["score"], "score", f"{path}:{number}"),
                    "target": _finite_float(row["target"], "target", f"{path}:{number}"),
                }
            )
        return records


def _variant_id(case_id: str, seed: str) -> str:
    return f"{case_id}::seed={seed}"


def _daily_maps(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, float | None]]]:
    result: dict[str, dict[str, dict[str, float | None]]] = defaultdict(dict)
    for row in records:
        result[str(row["date"])][str(row["asset"])] = {
            "score": row["score"],
            "target": row["target"],
        }
    return dict(result)


def _daily_correlations(
    daily: Mapping[str, Mapping[str, Mapping[str, float | None]]]
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    pearson_by_date: dict[str, float | None] = {}
    rank_by_date: dict[str, float | None] = {}
    for date, assets in sorted(daily.items()):
        pairs = [
            (item["score"], item["target"])
            for item in assets.values()
            if item["score"] is not None and item["target"] is not None
        ]
        scores = [pair[0] for pair in pairs]
        targets = [pair[1] for pair in pairs]
        pearson_by_date[date] = pearson(scores, targets)
        rank_by_date[date] = spearman(scores, targets)
    return pearson_by_date, rank_by_date


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": mean(values),
        "std": population_std(values),
        "q10": quantile(values, 0.10),
        "q50": quantile(values, 0.50),
        "q90": quantile(values, 0.90),
    }


def _find_non_monotonic_points(
    ordered_variants: Sequence[str],
    per_variant: Mapping[str, Any],
    paired: Mapping[str, Any],
    tolerance: float,
) -> dict[str, Any]:
    """Check preregistered perturbation order for expected smooth degradation."""

    metric_specs = {
        "daily_rank_ic_median": ("non_increasing", lambda variant: per_variant[variant]["daily_rank_ic"]["median"]),
        "top_k_jaccard_median": ("non_increasing", lambda variant: paired[variant]["top_k_jaccard"]["median"]),
        "score_rank_correlation_median": ("non_increasing", lambda variant: paired[variant]["score_rank_correlation"]["median"]),
        "signal_flip_rate_median": ("non_decreasing", lambda variant: paired[variant]["signal_flip_rate"]["median"]),
    }
    result: dict[str, Any] = {}
    for metric, (expected, getter) in metric_specs.items():
        points = [{"variant": variant, "value": getter(variant)} for variant in ordered_variants]
        violations: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for point in points:
            if point["value"] is None:
                continue
            if previous is not None:
                if expected == "non_increasing":
                    violated = point["value"] > previous["value"] + tolerance
                else:
                    violated = point["value"] < previous["value"] - tolerance
                if violated:
                    violations.append({"previous": previous, "current": point})
            previous = point
        result[metric] = {"expected": expected, "points": points, "non_monotonic_points": violations}
    return result


def compute_stability_metrics(
    records: Sequence[Mapping[str, Any]],
    baseline_case: str,
    baseline_seed: str,
    top_k: int = 10,
    worst_quantile: float = 0.10,
    ordered_variants: Sequence[str] | None = None,
    monotonic_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Compute per-variant IC summaries and paired stability vs a baseline."""

    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if not 0.0 <= worst_quantile <= 0.5:
        raise ValueError("worst_quantile must be between 0 and 0.5")
    if monotonic_tolerance < 0.0:
        raise ValueError("monotonic_tolerance must be non-negative")

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[_variant_id(str(row["case_id"]), str(row["seed"]))].append(row)
    baseline_id = _variant_id(baseline_case, baseline_seed)
    if baseline_id not in grouped:
        raise ValueError(f"baseline variant not found: {baseline_id}")

    daily_by_variant = {variant: _daily_maps(rows) for variant, rows in grouped.items()}
    correlations: dict[str, tuple[dict[str, float | None], dict[str, float | None]]] = {}
    per_variant: dict[str, Any] = {}
    warnings: list[dict[str, str]] = []

    for variant, rows in sorted(grouped.items()):
        pearson_by_date, rank_by_date = _daily_correlations(daily_by_variant[variant])
        correlations[variant] = (pearson_by_date, rank_by_date)
        scores = [row["score"] for row in rows if row["score"] is not None]
        missing_count = len(rows) - len(scores)
        per_variant[variant] = {
            "row_count": len(rows),
            "missing_score_count": missing_count,
            "missing_coverage": missing_count / len(rows) if rows else None,
            "score_distribution": _distribution(scores),
            "daily_pearson_ic": summarize(pearson_by_date.values(), worst_quantile),
            "daily_rank_ic": summarize(rank_by_date.values(), worst_quantile),
        }
        if not any(value is not None for value in rank_by_date.values()):
            warnings.append({"variant": variant, "code": "NO_VALID_RANK_IC", "message": "targets are missing, insufficient, or constant"})

    baseline_daily = daily_by_variant[baseline_id]
    baseline_rank = correlations[baseline_id][1]
    baseline_distribution = per_variant[baseline_id]["score_distribution"]
    paired: dict[str, Any] = {}

    for variant, daily in sorted(daily_by_variant.items()):
        jaccards: list[float | None] = []
        rank_correlations: list[float | None] = []
        flip_rates: list[float | None] = []
        common_counts: list[int] = []
        common_dates = sorted(set(baseline_daily) & set(daily))
        for date in common_dates:
            baseline_scores = {
                asset: values["score"]
                for asset, values in baseline_daily[date].items()
                if values["score"] is not None
            }
            variant_scores = {
                asset: values["score"]
                for asset, values in daily[date].items()
                if values["score"] is not None
            }
            jaccards.append(jaccard(top_k_assets(baseline_scores, top_k), top_k_assets(variant_scores, top_k)))
            common_assets = sorted(set(baseline_scores) & set(variant_scores))
            common_counts.append(len(common_assets))
            left = [baseline_scores[asset] for asset in common_assets]
            right = [variant_scores[asset] for asset in common_assets]
            rank_correlations.append(spearman(left, right))
            if common_assets:
                flips = sum(
                    (baseline_scores[asset] > 0) != (variant_scores[asset] > 0)
                    for asset in common_assets
                )
                flip_rates.append(flips / len(common_assets))
            else:
                flip_rates.append(None)

        variant_rank = correlations[variant][1]
        sign_dates = sorted(
            date
            for date in set(baseline_rank) & set(variant_rank)
            if baseline_rank[date] is not None and variant_rank[date] is not None
        )
        sign_retention = (
            sum((baseline_rank[date] >= 0) == (variant_rank[date] >= 0) for date in sign_dates)
            / len(sign_dates)
            if sign_dates
            else None
        )

        distribution = per_variant[variant]["score_distribution"]
        mean_delta = None
        if distribution["mean"] is not None and baseline_distribution["mean"] is not None:
            mean_delta = distribution["mean"] - baseline_distribution["mean"]
        std_ratio = None
        if distribution["std"] is not None and baseline_distribution["std"] not in {None, 0.0}:
            std_ratio = distribution["std"] / baseline_distribution["std"]
        quantile_deltas = {}
        for key in ("q10", "q50", "q90"):
            left_value = baseline_distribution[key]
            right_value = distribution[key]
            quantile_deltas[key] = None if left_value is None or right_value is None else right_value - left_value

        paired[variant] = {
            "common_date_count": len(common_dates),
            "minimum_common_assets": min(common_counts) if common_counts else 0,
            "top_k_jaccard": summarize(jaccards, worst_quantile),
            "score_rank_correlation": summarize(rank_correlations, worst_quantile),
            "signal_flip_rate": summarize(flip_rates, worst_quantile, direction="lower"),
            "rank_ic_sign_retention_rate": sign_retention,
            "prediction_distribution_drift": {
                "mean_delta": mean_delta,
                "std_ratio": std_ratio,
                "quantile_deltas": quantile_deltas,
            },
        }
        if not common_dates:
            warnings.append({"variant": variant, "code": "NO_COMMON_DATES", "message": "variant has no dates in common with baseline"})

    degradation_curve = None
    if ordered_variants:
        unknown = [variant for variant in ordered_variants if variant not in grouped]
        if unknown:
            raise ValueError(f"ordered_variants not found: {', '.join(unknown)}")
        degradation_curve = _find_non_monotonic_points(
            ordered_variants, per_variant, paired, monotonic_tolerance
        )

    return {
        "baseline_variant": baseline_id,
        "top_k": top_k,
        "worst_quantile_probability": worst_quantile,
        "per_variant": per_variant,
        "paired_vs_baseline": paired,
        "degradation_curve": degradation_curve,
        "warnings": warnings,
        "threshold_verdict": "INCONCLUSIVE",
        "threshold_note": "This script computes evidence only; apply preregistered thresholds externally.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, help="CSV: date,asset,case_id,seed,score,target")
    parser.add_argument("--baseline-case", required=True, help="case_id of the paired baseline")
    parser.add_argument("--baseline-seed", default="", help="seed of the paired baseline; default is empty")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--worst-quantile", type=float, default=0.10)
    parser.add_argument(
        "--ordered-variants",
        help="comma-separated case_id::seed=<seed> sequence for ML-011 monotonicity checks",
    )
    parser.add_argument("--monotonic-tolerance", type=float, default=0.0)
    parser.add_argument("--output", help="JSON output path; stdout when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = read_predictions(args.predictions)
        ordered_variants = (
            [item.strip() for item in args.ordered_variants.split(",") if item.strip()]
            if args.ordered_variants
            else None
        )
        result = compute_stability_metrics(
            records,
            baseline_case=args.baseline_case,
            baseline_seed=args.baseline_seed,
            top_k=args.top_k,
            worst_quantile=args.worst_quantile,
            ordered_variants=ordered_variants,
            monotonic_tolerance=args.monotonic_tolerance,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
