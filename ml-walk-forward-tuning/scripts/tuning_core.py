#!/usr/bin/env python3
"""Core contracts for deterministic walk-forward tuning plans and summaries."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


class ContractError(ValueError):
    """Raised when a study artifact violates the public contract."""


REQUIRED_DIMENSIONS = {
    "factor_set",
    "label",
    "selection_rule",
    "holding_rule",
    "execution_rule",
}
EARLY_STOPPING_MODES = {"disabled", "inner_validation", "outer_validation"}
RESULT_FIELDS = [
    "trial_id",
    "candidate_id",
    "validation_id",
    "seed",
    "metric",
    "value",
    "status",
    "artifact_path",
    "error",
    "actual_iterations",
]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_iso_date(raw: Any, field: str) -> date:
    if not isinstance(raw, str):
        raise ContractError(f"{field} must be an ISO date string")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{field} must use YYYY-MM-DD: {raw!r}") from exc


def require_nonempty_string(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return raw.strip()


def require_nonnegative_int(raw: Any, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ContractError(f"{field} must be a non-negative integer")
    return raw


def validate_json_value(value: Any, field: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ContractError(f"{field} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, f"{field}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{field} contains a non-string object key")
            validate_json_value(item, f"{field}.{key}")
        return
    raise ContractError(f"{field} contains unsupported JSON value {type(value).__name__}")


def require_unique_json(values: list[Any], field: str) -> None:
    encoded = [canonical_json(value) for value in values]
    if len(encoded) != len(set(encoded)):
        raise ContractError(f"{field} contains duplicate values")


def shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle, parse_constant=lambda token: (_ for _ in ()).throw(
                ContractError(f"{path} contains invalid JSON number {token}")
            ))
    except json.JSONDecodeError as exc:
        raise ContractError(f"Invalid JSON in {path}: {exc}") from exc


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("study config must be a JSON object")
    validate_json_value(raw, "config")
    study_id = require_nonempty_string(raw.get("study_id"), "study_id")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise ContractError("data must be an object")
    available_start = parse_iso_date(data.get("available_start"), "data.available_start")
    available_end = parse_iso_date(data.get("available_end"), "data.available_end")
    if available_start > available_end:
        raise ContractError("data.available_start must not exceed data.available_end")
    for field in ("frequency", "calendar", "timezone"):
        require_nonempty_string(data.get(field), f"data.{field}")
    label_horizon = require_nonnegative_int(
        data.get("label_horizon_observations"), "data.label_horizon_observations"
    )
    purge = require_nonnegative_int(data.get("purge_observations"), "data.purge_observations")
    embargo = require_nonnegative_int(data.get("embargo_observations"), "data.embargo_observations")
    if purge < label_horizon:
        raise ContractError("data.purge_observations must be >= data.label_horizon_observations")

    development = raw.get("development")
    if not isinstance(development, dict):
        raise ContractError("development must be an object")
    periods_raw = development.get("validation_periods")
    if not isinstance(periods_raw, list) or not periods_raw:
        raise ContractError("development.validation_periods must be a non-empty list")
    periods: list[dict[str, Any]] = []
    period_ids: set[str] = set()
    previous_end: date | None = None
    for index, item in enumerate(periods_raw):
        if not isinstance(item, dict):
            raise ContractError(f"development.validation_periods[{index}] must be an object")
        period_id = require_nonempty_string(item.get("id"), f"validation_periods[{index}].id")
        if period_id in period_ids:
            raise ContractError(f"duplicate validation period id: {period_id}")
        period_ids.add(period_id)
        start = parse_iso_date(item.get("start"), f"validation_periods[{index}].start")
        end = parse_iso_date(item.get("end"), f"validation_periods[{index}].end")
        if start > end:
            raise ContractError(f"validation period {period_id} starts after it ends")
        if start < available_start or end > available_end:
            raise ContractError(f"validation period {period_id} is outside data coverage")
        if previous_end is not None and start <= previous_end:
            raise ContractError("validation periods must be ordered and non-overlapping")
        previous_end = end
        periods.append({"id": period_id, "start": start.isoformat(), "end": end.isoformat()})

    windows_raw = development.get("training_windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ContractError("development.training_windows must be a non-empty list")
    windows: list[dict[str, Any]] = []
    window_ids: set[str] = set()
    for index, item in enumerate(windows_raw):
        if not isinstance(item, dict):
            raise ContractError(f"development.training_windows[{index}] must be an object")
        window_id = require_nonempty_string(item.get("id"), f"training_windows[{index}].id")
        if window_id in window_ids:
            raise ContractError(f"duplicate training window id: {window_id}")
        window_ids.add(window_id)
        length = item.get("length")
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise ContractError(f"training window {window_id} length must be a positive integer")
        if item.get("unit") != "calendar_years":
            raise ContractError(f"training window {window_id} unit must be calendar_years")
        windows.append({"id": window_id, "length": length, "unit": "calendar_years"})

    factor_sets = raw.get("factor_sets")
    if not isinstance(factor_sets, dict) or not factor_sets:
        raise ContractError("factor_sets must be a non-empty object")
    normalized_factor_sets: dict[str, list[str]] = {}
    for name, factors in factor_sets.items():
        factor_set_id = require_nonempty_string(name, "factor_sets key")
        if not isinstance(factors, list) or not factors:
            raise ContractError(f"factor_sets.{factor_set_id} must be a non-empty list")
        normalized_factors = [
            require_nonempty_string(factor, f"factor_sets.{factor_set_id}") for factor in factors
        ]
        if len(normalized_factors) != len(set(normalized_factors)):
            raise ContractError(f"factor_sets.{factor_set_id} contains duplicate factors")
        normalized_factor_sets[factor_set_id] = normalized_factors

    fixed = raw.get("fixed")
    search = raw.get("search")
    if not isinstance(fixed, dict) or not isinstance(search, dict):
        raise ContractError("fixed and search must both be objects")
    overlap = set(fixed) & set(search)
    if overlap:
        raise ContractError(f"fixed and search dimensions overlap: {', '.join(sorted(overlap))}")
    declared = set(fixed) | set(search)
    missing_dimensions = REQUIRED_DIMENSIONS - declared
    if missing_dimensions:
        raise ContractError(f"missing required dimensions: {', '.join(sorted(missing_dimensions))}")
    for key, value in fixed.items():
        require_nonempty_string(key, "fixed dimension name")
        validate_json_value(value, f"fixed.{key}")
    normalized_search: dict[str, list[Any]] = {}
    for key, values in search.items():
        require_nonempty_string(key, "search dimension name")
        if not isinstance(values, list) or not values:
            raise ContractError(f"search.{key} must be a non-empty list")
        for index, value in enumerate(values):
            validate_json_value(value, f"search.{key}[{index}]")
        require_unique_json(values, f"search.{key}")
        normalized_search[key] = values
    factor_values = [fixed["factor_set"]] if "factor_set" in fixed else normalized_search["factor_set"]
    for factor_set_id in factor_values:
        if not isinstance(factor_set_id, str) or factor_set_id not in normalized_factor_sets:
            raise ContractError(f"factor_set references unknown explicit set: {factor_set_id!r}")

    model_spaces_raw = raw.get("model_spaces")
    if not isinstance(model_spaces_raw, list) or not model_spaces_raw:
        raise ContractError("model_spaces must be a non-empty list")
    model_spaces: list[dict[str, Any]] = []
    model_ids: set[str] = set()
    for index, item in enumerate(model_spaces_raw):
        if not isinstance(item, dict):
            raise ContractError(f"model_spaces[{index}] must be an object")
        model_id = require_nonempty_string(item.get("id"), f"model_spaces[{index}].id")
        family = require_nonempty_string(item.get("family"), f"model_spaces[{index}].family")
        if model_id in model_ids:
            raise ContractError(f"duplicate model space id: {model_id}")
        model_ids.add(model_id)
        fixed_params = item.get("fixed_params", {})
        search_params = item.get("search_params", {})
        if not isinstance(fixed_params, dict) or not isinstance(search_params, dict):
            raise ContractError(f"model space {model_id} params must be objects")
        param_overlap = set(fixed_params) & set(search_params)
        if param_overlap:
            raise ContractError(
                f"model space {model_id} fixed/search params overlap: {', '.join(sorted(param_overlap))}"
            )
        normalized_param_search: dict[str, list[Any]] = {}
        for key, value in fixed_params.items():
            require_nonempty_string(key, f"model space {model_id} fixed param")
            validate_json_value(value, f"model_spaces.{model_id}.fixed_params.{key}")
        for key, values in search_params.items():
            require_nonempty_string(key, f"model space {model_id} search param")
            if not isinstance(values, list) or not values:
                raise ContractError(f"model space {model_id} search param {key} must be a non-empty list")
            for param_index, value in enumerate(values):
                validate_json_value(value, f"model_spaces.{model_id}.search_params.{key}[{param_index}]")
            require_unique_json(values, f"model_spaces.{model_id}.search_params.{key}")
            normalized_param_search[key] = values
        early_stopping = item.get("early_stopping")
        if not isinstance(early_stopping, dict):
            raise ContractError(f"model space {model_id} early_stopping must be an object")
        mode = early_stopping.get("mode")
        if mode not in EARLY_STOPPING_MODES:
            raise ContractError(f"model space {model_id} has invalid early_stopping.mode")
        normalized_early_stopping: dict[str, Any] = {"mode": mode}
        if mode != "disabled":
            rounds = early_stopping.get("rounds")
            if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds <= 0:
                raise ContractError(f"model space {model_id} early_stopping.rounds must be positive")
            normalized_early_stopping["rounds"] = rounds
        elif set(early_stopping) - {"mode"}:
            raise ContractError(f"model space {model_id} disabled early stopping cannot have extra fields")
        model_spaces.append(
            {
                "id": model_id,
                "family": family,
                "fixed_params": fixed_params,
                "search_params": normalized_param_search,
                "early_stopping": normalized_early_stopping,
            }
        )

    seeds_raw = raw.get("seeds")
    if not isinstance(seeds_raw, list) or not seeds_raw:
        raise ContractError("seeds must be a non-empty list")
    seeds = [require_nonnegative_int(seed, "seed") for seed in seeds_raw]
    if len(seeds) != len(set(seeds)):
        raise ContractError("seeds contains duplicate values")

    metrics_raw = raw.get("metrics")
    if not isinstance(metrics_raw, list) or not metrics_raw:
        raise ContractError("metrics must be a non-empty list")
    metrics: list[dict[str, str]] = []
    metric_names: set[str] = set()
    for index, item in enumerate(metrics_raw):
        if not isinstance(item, dict):
            raise ContractError(f"metrics[{index}] must be an object")
        name = require_nonempty_string(item.get("name"), f"metrics[{index}].name")
        direction = item.get("direction")
        if direction not in {"max", "min"}:
            raise ContractError(f"metric {name} direction must be max or min")
        if name in metric_names:
            raise ContractError(f"duplicate metric name: {name}")
        metric_names.add(name)
        metrics.append({"name": name, "direction": direction})

    normalized_config = {
        "study_id": study_id,
        "data": {
            "available_start": available_start.isoformat(),
            "available_end": available_end.isoformat(),
            "frequency": data["frequency"],
            "calendar": data["calendar"],
            "timezone": data["timezone"],
            "label_horizon_observations": label_horizon,
            "purge_observations": purge,
            "embargo_observations": embargo,
        },
        "development": {"validation_periods": periods, "training_windows": windows},
        "factor_sets": normalized_factor_sets,
        "fixed": fixed,
        "search": normalized_search,
        "model_spaces": model_spaces,
        "seeds": seeds,
        "metrics": metrics,
    }
    return normalized_config


def expand_mapping(fixed: dict[str, Any], search: dict[str, list[Any]]) -> Iterable[dict[str, Any]]:
    keys = sorted(search)
    if not keys:
        yield dict(fixed)
        return
    for values in itertools.product(*(search[key] for key in keys)):
        result = dict(fixed)
        result.update(zip(keys, values))
        yield result


def build_plan(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    available_start = parse_iso_date(config["data"]["available_start"], "available_start")
    periods = config["development"]["validation_periods"]
    windows = config["development"]["training_windows"]
    dimension_variants = list(expand_mapping(config["fixed"], config["search"]))
    model_variants: list[dict[str, Any]] = []
    model_cardinalities: dict[str, int] = {}
    for model_space in config["model_spaces"]:
        variants = list(expand_mapping(model_space["fixed_params"], model_space["search_params"]))
        model_cardinalities[model_space["id"]] = len(variants)
        for params in variants:
            model_variants.append(
                {
                    "space_id": model_space["id"],
                    "family": model_space["family"],
                    "params": params,
                    "early_stopping": model_space["early_stopping"],
                }
            )

    candidates: list[dict[str, Any]] = []
    candidate_payloads: dict[str, str] = {}
    for window in windows:
        for dimensions in dimension_variants:
            for model in model_variants:
                candidate_config = {
                    "training_window": window,
                    "dimensions": dimensions,
                    "model": model,
                }
                candidate_id = f"cand_{stable_hash({'study_id': config['study_id'], 'config': candidate_config})[:16]}"
                encoded = canonical_json(candidate_config)
                if candidate_id in candidate_payloads and candidate_payloads[candidate_id] != encoded:
                    raise ContractError(f"candidate ID collision: {candidate_id}")
                candidate_payloads[candidate_id] = encoded
                candidates.append({"candidate_id": candidate_id, "config": candidate_config})

    trials: list[dict[str, Any]] = []
    trial_payloads: dict[str, str] = {}
    for candidate in candidates:
        window = candidate["config"]["training_window"]
        for period in periods:
            validation_start = parse_iso_date(period["start"], f"validation {period['id']} start")
            training_start = shift_years(validation_start, window["length"])
            training_end = validation_start - timedelta(days=1)
            if training_start < available_start:
                raise ContractError(
                    f"training window {window['id']} for validation {period['id']} starts "
                    f"{training_start.isoformat()} before data.available_start {available_start.isoformat()}"
                )
            if training_end >= validation_start:
                raise ContractError(f"training/validation overlap for {window['id']} and {period['id']}")
            for seed in config["seeds"]:
                identity = {
                    "study_id": config["study_id"],
                    "candidate_id": candidate["candidate_id"],
                    "validation_id": period["id"],
                    "seed": seed,
                }
                trial_id = f"trial_{stable_hash(identity)[:16]}"
                encoded = canonical_json(identity)
                if trial_id in trial_payloads and trial_payloads[trial_id] != encoded:
                    raise ContractError(f"trial ID collision: {trial_id}")
                trial_payloads[trial_id] = encoded
                trials.append(
                    {
                        "trial_id": trial_id,
                        "candidate_id": candidate["candidate_id"],
                        "validation_id": period["id"],
                        "seed": seed,
                        "training": {
                            "start": training_start.isoformat(),
                            "end": training_end.isoformat(),
                            "purge_observations": config["data"]["purge_observations"],
                            "embargo_observations": config["data"]["embargo_observations"],
                        },
                        "validation": {"start": period["start"], "end": period["end"]},
                        "candidate_config": candidate["config"],
                        "metrics": config["metrics"],
                    }
                )

    dimension_cardinalities = {key: len(values) for key, values in config["search"].items()}
    dimension_cardinalities.update({key: 1 for key in config["fixed"]})
    metadata = {
        "candidate_count": len(candidates),
        "trial_count": len(trials),
        "validation_period_count": len(periods),
        "seed_count": len(config["seeds"]),
        "dimension_cardinalities": dict(sorted(dimension_cardinalities.items())),
        "model_variant_counts": model_cardinalities,
    }
    return candidates, trials, metadata


def ensure_empty_output_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ContractError(f"output path exists and is not a directory: {path}")
        if any(path.iterdir()):
            raise ContractError(f"refusing to overwrite non-empty output directory: {path}")
    else:
        path.mkdir(parents=True)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ContractError(f"{path}:{line_number} is blank")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContractError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ContractError(f"{path}:{line_number} must contain a JSON object")
            rows.append(row)
    return rows


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def format_number(value: float | None) -> str:
    return "" if value is None else format(value, ".17g")


def mean_or_none(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def stdev_or_none(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def load_results(path: Path, trials: list[dict[str, Any]], metrics: list[dict[str, str]]) -> dict[str, Any]:
    trial_by_id = {trial["trial_id"]: trial for trial in trials}
    metric_names = {item["name"] for item in metrics}
    completed: dict[str, dict[str, Any]] = {}
    failed: dict[str, dict[str, Any]] = {}
    seen_keys: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_fields = set(RESULT_FIELDS) - set(reader.fieldnames or [])
        if missing_fields:
            raise ContractError(f"{path} is missing columns: {', '.join(sorted(missing_fields))}")
        for line_number, row in enumerate(reader, start=2):
            trial_id = (row.get("trial_id") or "").strip()
            if trial_id not in trial_by_id:
                raise ContractError(f"{path}:{line_number} references unknown trial_id {trial_id!r}")
            trial = trial_by_id[trial_id]
            if (row.get("candidate_id") or "").strip() != trial["candidate_id"]:
                raise ContractError(f"{path}:{line_number} candidate_id does not match the plan")
            if (row.get("validation_id") or "").strip() != trial["validation_id"]:
                raise ContractError(f"{path}:{line_number} validation_id does not match the plan")
            try:
                seed = int((row.get("seed") or "").strip())
            except ValueError as exc:
                raise ContractError(f"{path}:{line_number} seed must be an integer") from exc
            if seed != trial["seed"]:
                raise ContractError(f"{path}:{line_number} seed does not match the plan")
            status = (row.get("status") or "").strip()
            if status not in {"completed", "failed"}:
                raise ContractError(f"{path}:{line_number} status must be completed or failed")
            metric = (row.get("metric") or "").strip()
            value_raw = (row.get("value") or "").strip()
            artifact_path = (row.get("artifact_path") or "").strip()
            error = (row.get("error") or "").strip()
            iterations_raw = (row.get("actual_iterations") or "").strip()
            early_stopping_mode = trial["candidate_config"]["model"]["early_stopping"]["mode"]
            actual_iterations: int | None = None
            if iterations_raw:
                try:
                    actual_iterations = int(iterations_raw)
                except ValueError as exc:
                    raise ContractError(f"{path}:{line_number} actual_iterations must be an integer") from exc
                if actual_iterations <= 0:
                    raise ContractError(f"{path}:{line_number} actual_iterations must be positive")
            if status == "completed" and early_stopping_mode != "disabled" and actual_iterations is None:
                raise ContractError(f"{path}:{line_number} must record actual_iterations for enabled early stopping")

            if status == "failed":
                if metric or value_raw:
                    raise ContractError(f"{path}:{line_number} failed row must have blank metric and value")
                if not error:
                    raise ContractError(f"{path}:{line_number} failed row must include error")
                if trial_id in completed or trial_id in failed:
                    raise ContractError(f"{path}:{line_number} duplicates result for trial {trial_id}")
                failed[trial_id] = {
                    "artifact_path": artifact_path,
                    "error": error,
                    "actual_iterations": actual_iterations,
                }
                continue

            if metric not in metric_names:
                raise ContractError(f"{path}:{line_number} references unknown metric {metric!r}")
            key = (trial_id, metric)
            if key in seen_keys or trial_id in failed:
                raise ContractError(f"{path}:{line_number} duplicates or conflicts for trial {trial_id}, metric {metric}")
            seen_keys.add(key)
            try:
                value = float(value_raw)
            except ValueError as exc:
                raise ContractError(f"{path}:{line_number} value must be numeric") from exc
            if not math.isfinite(value):
                raise ContractError(f"{path}:{line_number} value must be finite")
            state = completed.setdefault(
                trial_id,
                {
                    "metrics": {},
                    "artifact_path": artifact_path,
                    "actual_iterations": actual_iterations,
                },
            )
            if state["artifact_path"] != artifact_path or state["actual_iterations"] != actual_iterations:
                raise ContractError(f"{path}:{line_number} trial metadata differs across metric rows")
            state["metrics"][metric] = value

    incomplete_completed: dict[str, list[str]] = {}
    for trial_id, state in completed.items():
        missing = sorted(metric_names - set(state["metrics"]))
        if missing:
            incomplete_completed[trial_id] = missing
    return {"completed": completed, "failed": failed, "incomplete_completed": incomplete_completed}


def pareto_ids(candidate_values: dict[str, dict[str, float]], metrics: list[dict[str, str]]) -> list[str]:
    result: list[str] = []
    for candidate_id, values in sorted(candidate_values.items()):
        dominated = False
        for other_id, other in candidate_values.items():
            if other_id == candidate_id:
                continue
            at_least_as_good = True
            strictly_better = False
            for metric in metrics:
                name = metric["name"]
                if metric["direction"] == "max":
                    at_least_as_good &= other[name] >= values[name]
                    strictly_better |= other[name] > values[name]
                else:
                    at_least_as_good &= other[name] <= values[name]
                    strictly_better |= other[name] < values[name]
            if at_least_as_good and strictly_better:
                dominated = True
                break
        if not dominated:
            result.append(candidate_id)
    return result
