#!/usr/bin/env python3
"""Deterministic helpers for adaptive factor-model study planning."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class ContractError(ValueError):
    """Raised when an input violates the frozen study contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_id(prefix: str, value: Any, length: int = 16) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:length]}"


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_output_dir(path: str | Path) -> Path:
    target = Path(path).resolve()
    if target.exists() and any(target.iterdir()):
        raise ContractError(f"output directory must not exist or must be empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ContractError(f"JSONL row {line_no} must be an object")
            rows.append(value)
    return rows


def write_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical_json(dict(row)) + "\n")


def percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _as_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def profile_csv(
    path: str | Path,
    *,
    decision_column: str,
    label_column: str,
    feature_columns: Sequence[str],
) -> dict[str, Any]:
    source = Path(path).resolve()
    if source.suffix.lower() != ".csv":
        raise ContractError("the built-in profiler accepts CSV; use a target-repository adapter for Parquet/SQL")
    decision_counts: Counter[str] = Counter()
    label_values: list[float] = []
    label_counts: Counter[str] = Counter()
    missing = Counter({name: 0 for name in feature_columns})
    row_count = 0
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        required = {decision_column, label_column, *feature_columns}
        absent = sorted(required.difference(columns))
        if absent:
            raise ContractError(f"dataset is missing required columns: {absent}")
        for row in reader:
            row_count += 1
            decision = str(row.get(decision_column, "")).strip()
            if not decision:
                raise ContractError(f"empty decision value at data row {row_count + 1}")
            decision_counts[decision] += 1
            raw_label = str(row.get(label_column, "")).strip()
            if raw_label:
                label_counts[raw_label] += 1
                number = _as_float(raw_label)
                if number is not None:
                    label_values.append(number)
            for name in feature_columns:
                if _as_float(row.get(name)) is None:
                    missing[name] += 1
    if not row_count:
        raise ContractError("dataset contains no rows")
    group_sizes = [float(value) for value in decision_counts.values()]
    label_mean = sum(label_values) / len(label_values) if label_values else None
    label_std = None
    if len(label_values) > 1 and label_mean is not None:
        label_std = math.sqrt(sum((x - label_mean) ** 2 for x in label_values) / (len(label_values) - 1))
    return {
        "dataset": str(source),
        "dataset_sha256": file_sha256(source),
        "n_rows": row_count,
        "n_decisions": len(decision_counts),
        "decision_min": min(decision_counts) if decision_counts else None,
        "decision_max": max(decision_counts) if decision_counts else None,
        "group_size_p10": percentile(group_sizes, 0.10),
        "group_size_median": percentile(group_sizes, 0.50),
        "group_size_p90": percentile(group_sizes, 0.90),
        "n_features": len(feature_columns),
        "rows_per_feature": row_count / max(1, len(feature_columns)),
        "decisions_per_feature": len(decision_counts) / max(1, len(feature_columns)),
        "feature_missing_ratio": {name: missing[name] / row_count for name in feature_columns},
        "label_non_null": sum(label_counts.values()),
        "label_unique": len(label_counts),
        "label_mean": label_mean,
        "label_std": label_std,
        "label_p01": percentile(label_values, 0.01),
        "label_p50": percentile(label_values, 0.50),
        "label_p99": percentile(label_values, 0.99),
        "largest_label_share": max(label_counts.values(), default=0) / max(1, sum(label_counts.values())),
    }


def recommend_spec(profile: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    n_rows = int(profile["n_rows"])
    n_decisions = int(profile["n_decisions"])
    n_features = int(profile["n_features"])
    if n_decisions < 252 or n_rows < 20_000:
        regime = "sparse"
        params: dict[str, Any] = {"max_depth": 3, "learning_rate": 0.03, "max_rounds": 1500,
                  "subsample": 1.0, "min_child_weight": 5, "regularization": "strong"}
    elif n_decisions >= 1000 and n_rows >= 200_000:
        regime = "large"
        params = {"max_depth": 6, "learning_rate": 0.02, "max_rounds": 3000,
                  "subsample": 0.8, "min_child_weight": 20, "regularization": "medium"}
    else:
        regime = "standard"
        params = {"max_depth": 5, "learning_rate": 0.03, "max_rounds": 2000,
                  "subsample": 0.8, "min_child_weight": 10, "regularization": "medium"}
    params["colsample_bytree"] = 1.0 if n_features <= 20 else (0.8 if n_features <= 100 else 0.7)
    params["early_stopping_rounds"] = min(200, max(50, round(int(params["max_rounds"]) * 0.05)))
    requested_task = contract.get("task_type")
    task_type = requested_task or "requires_user_confirmation"
    open_questions = []
    if not requested_task:
        open_questions.append("Confirm whether the strategy target is ranking, regression, or classification.")
    if not contract.get("primary_metric"):
        open_questions.append("Confirm the primary inner-validation metric and its strategy-aligned cutoff.")
    if not contract.get("resource_budget"):
        open_questions.append("Confirm runtime, memory, hardware, concurrency, and seed budget.")
    if not contract.get("account_adapter"):
        open_questions.append("Identify the formal strategy account/backtest adapter.")
    return {
        "schema": "adaptive-factor-model-training/recommendation-v1",
        "task_type": task_type,
        "data_regime": regime,
        "model": {
            "family": contract.get("model_family", "gradient_boosted_trees"),
            "params": params,
            "recommendation_only": True,
        },
        "inner_validation": {
            "mode": "last_fraction_of_training_decisions",
            "fraction": 0.20,
            "embargo": "derive_from_actual_label_path_and_decision_interval",
            "rebuild_factor_route": True,
        },
        "factor_routes": contract.get("factor_routes", ["raw", "hierarchical_average"]),
        "seed_budget": contract.get("seed_budget", "confirm_with_user"),
        "open_questions": open_questions,
        "rationale": [
            f"regime={regime} from n_rows={n_rows} and n_decisions={n_decisions}",
            f"colsample recommendation uses n_features={n_features}",
            "all recommendations require explicit user confirmation before plan freezing",
        ],
    }


def validate_confirmed_spec(spec: Mapping[str, Any], recommended_hash: str) -> None:
    if spec.get("user_confirmed") is not True:
        raise ContractError("confirmed spec must set user_confirmed=true")
    if spec.get("recommended_spec_hash") != recommended_hash:
        raise ContractError("confirmed spec does not reference the current recommended_spec hash")
    required = ["task_type", "model", "outer_folds", "seeds", "preprocessing", "model_adapter"]
    missing = [key for key in required if key not in spec]
    if missing:
        raise ContractError(f"confirmed spec is missing required fields: {missing}")
    if spec["task_type"] not in {"cross_sectional_ranking", "return_regression", "classification"}:
        raise ContractError("unsupported task_type")
    seeds = spec["seeds"]
    if not isinstance(seeds, list) or not seeds or len(seeds) != len(set(seeds)):
        raise ContractError("seeds must be a non-empty unique list")
    folds = spec["outer_folds"]
    if not isinstance(folds, list) or not folds:
        raise ContractError("outer_folds must be a non-empty list")
    for fold in folds:
        for key in ("fold_id", "train_start", "train_end", "validation_start", "validation_end"):
            if key not in fold:
                raise ContractError(f"outer fold is missing {key}")
        if not (fold["train_start"] <= fold["train_end"] < fold["validation_start"] <= fold["validation_end"]):
            raise ContractError(f"outer fold {fold['fold_id']} has invalid time order")
    command = spec["model_adapter"].get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ContractError("model_adapter.command must be a non-empty argv string list")


def validate_routes(routes: Sequence[Mapping[str, Any]]) -> None:
    if not routes:
        raise ContractError("route manifest is empty")
    route_ids = [row.get("route_id") for row in routes]
    if any(not value for value in route_ids) or len(route_ids) != len(set(route_ids)):
        raise ContractError("route_id values must be present and unique")
    allowed = {"raw", "greedy", "hierarchical_average", "hierarchical_single", "hierarchical_complete"}
    for route in routes:
        if route.get("method") not in allowed:
            raise ContractError(f"unsupported route method: {route.get('method')}")
        if not route.get("screening_recipe_id"):
            raise ContractError(f"route {route['route_id']} lacks screening_recipe_id")


def plan_trials(spec: Mapping[str, Any], routes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    for fold in spec["outer_folds"]:
        for route in routes:
            for seed in spec["seeds"]:
                payload = {"fold_id": fold["fold_id"], "route_id": route["route_id"], "seed": seed}
                trials.append({
                    "trial_id": content_id("trial", payload),
                    **payload,
                    "fold": fold,
                    "route": route,
                    "task_type": spec["task_type"],
                    "model": spec["model"],
                    "preprocessing": spec["preprocessing"],
                    "early_stopping": spec.get("early_stopping", {}),
                })
    return trials


def format_command(template: Sequence[str], replacements: Mapping[str, str]) -> list[str]:
    result: list[str] = []
    for token in template:
        formatted = token
        for key, value in replacements.items():
            formatted = formatted.replace("{" + key + "}", value)
        if "{" in formatted or "}" in formatted:
            raise ContractError(f"command contains unresolved or unsupported placeholder: {formatted}")
        result.append(formatted)
    if not result or not result[0]:
        raise ContractError("resolved command is empty")
    return result


def write_csv(path: str | Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def environment_manifest() -> dict[str, Any]:
    return {
        "python": sys.version,
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
    }
