#!/usr/bin/env python3
"""Deterministic calculations for the optimize-strategy-portfolios skill."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


SCHEMA_VERSION = 1
RISK_KEYS = ("ordinary_beta", "controlled_beta", "downside_beta", "tail_10_beta")


class ContractError(ValueError):
    """Raised when an input or frozen rule violates the experiment contract."""


def parse_date(value: str, field: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be ISO YYYY-MM-DD, got {value!r}") from exc


def parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise ContractError(f"{field} must be boolean, got {value!r}")


def finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be numeric, got {value!r}") from exc
    if not math.isfinite(result):
        raise ContractError(f"{field} must be finite, got {value!r}")
    return result


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prepare_output_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ContractError(f"Output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def require_columns(reader: csv.DictReader, required: set[str], path: Path) -> None:
    missing = sorted(required - set(reader.fieldnames or []))
    if missing:
        raise ContractError(f"{path} missing columns: {', '.join(missing)}")


def read_return_panel(path: Path, id_field: str) -> dict[str, dict[date, float]]:
    panel: dict[str, dict[date, float]] = defaultdict(dict)
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, {id_field, "date", "net_return"}, path)
        for line, row in enumerate(reader, 2):
            item_id = (row.get(id_field) or "").strip()
            if not item_id:
                raise ContractError(f"{path}:{line} blank {id_field}")
            day = parse_date(row["date"], f"{path}:{line}:date")
            if day in panel[item_id]:
                raise ContractError(f"{path}:{line} duplicate {id_field}+date")
            panel[item_id][day] = finite_float(row["net_return"], f"{path}:{line}:net_return")
    if not panel:
        raise ContractError(f"{path} contains no return rows")
    return dict(panel)


def read_diagnostics(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    required = {
        "deployment_id", "family_id", "candidate_id", "eligible", "quality_score",
        "trading_activity_ok", "coverage_complete", "positive_residual_periods",
        "residual_periods_total", "estimation_start", "estimation_end", "as_of",
        "rule_id", "source_artifact_path", "source_artifact_sha256",
    }
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, required, path)
        for line, row in enumerate(reader, 2):
            deployment = row["deployment_id"].strip()
            family_id = row["family_id"].strip()
            candidate = row["candidate_id"].strip()
            key = (deployment, candidate)
            if not all(key) or key in result:
                raise ContractError(f"{path}:{line} blank or duplicate deployment_id+candidate_id")
            try:
                positive_periods = int(row["positive_residual_periods"])
                total_periods = int(row["residual_periods_total"])
            except ValueError as exc:
                raise ContractError(f"{path}:{line} invalid positive_residual_periods") from exc
            result[key] = {
                "eligible": parse_bool(row["eligible"], f"{path}:{line}:eligible"),
                "family_id": family_id,
                "quality_score": finite_float(row["quality_score"], f"{path}:{line}:quality_score"),
                "trading_activity_ok": parse_bool(
                    row["trading_activity_ok"], f"{path}:{line}:trading_activity_ok"
                ),
                "coverage_complete": parse_bool(
                    row["coverage_complete"], f"{path}:{line}:coverage_complete"
                ),
                "positive_residual_periods": positive_periods,
                "residual_periods_total": total_periods,
                "estimation_start": parse_date(row["estimation_start"], f"{path}:{line}:estimation_start"),
                "estimation_end": parse_date(row["estimation_end"], f"{path}:{line}:estimation_end"),
                "as_of": parse_date(row["as_of"], f"{path}:{line}:as_of"),
                "rule_id": row["rule_id"].strip(),
                "source_artifact_path": (path.parent / row["source_artifact_path"]).resolve()
                if not Path(row["source_artifact_path"]).is_absolute()
                else Path(row["source_artifact_path"]).resolve(),
                "source_artifact_sha256": row["source_artifact_sha256"].strip(),
            }
            if not family_id or positive_periods < 0 or total_periods <= 0 or positive_periods > total_periods:
                raise ContractError(f"{path}:{line} invalid family or residual-period counts")
            if (
                not result[key]["rule_id"] or not row["source_artifact_path"].strip()
                or not is_sha256(result[key]["source_artifact_sha256"])
            ):
                raise ContractError(f"{path}:{line} missing rule_id or 64-character source hash")
    return result


def read_controls(path: Path | None) -> tuple[list[str], dict[date, list[float]]]:
    if path is None:
        return [], {}
    raw: dict[date, dict[str, float]] = defaultdict(dict)
    factor_ids: set[str] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, {"date", "factor_id", "return"}, path)
        for line, row in enumerate(reader, 2):
            day = parse_date(row["date"], f"{path}:{line}:date")
            factor = row["factor_id"].strip()
            if not factor or factor in raw[day]:
                raise ContractError(f"{path}:{line} blank or duplicate date+factor_id")
            raw[day][factor] = finite_float(row["return"], f"{path}:{line}:return")
            factor_ids.add(factor)
    ordered = sorted(factor_ids)
    controls: dict[date, list[float]] = {}
    for day, values in raw.items():
        if set(values) != factor_ids:
            raise ContractError(f"{path}: incomplete factor set on {day.isoformat()}")
        controls[day] = [values[factor] for factor in ordered]
    return ordered, controls


def read_clusters(path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if path is None:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, {
            "deployment_id", "candidate_id", "cluster_id", "estimation_start", "estimation_end",
            "as_of", "rule_id", "source_artifact_path", "source_artifact_sha256",
        }, path)
        for line, row in enumerate(reader, 2):
            key = (row["deployment_id"].strip(), row["candidate_id"].strip())
            cluster = row["cluster_id"].strip()
            if not all(key) or not cluster or key in result:
                raise ContractError(f"{path}:{line} blank or duplicate cluster row")
            rule_id = row["rule_id"].strip()
            source_hash = row["source_artifact_sha256"].strip()
            if not rule_id or not row["source_artifact_path"].strip() or not is_sha256(source_hash):
                raise ContractError(f"{path}:{line} missing cluster rule_id or 64-character source hash")
            result[key] = {
                "cluster_id": cluster,
                "estimation_start": parse_date(row["estimation_start"], f"{path}:{line}:estimation_start"),
                "estimation_end": parse_date(row["estimation_end"], f"{path}:{line}:estimation_end"),
                "as_of": parse_date(row["as_of"], f"{path}:{line}:as_of"),
                "rule_id": rule_id,
                "source_artifact_path": (path.parent / row["source_artifact_path"]).resolve()
                if not Path(row["source_artifact_path"]).is_absolute()
                else Path(row["source_artifact_path"]).resolve(),
                "source_artifact_sha256": source_hash,
            }
    return result


def read_prior_ledger(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    required = {
        "dataset_id", "access_order", "event_time", "purpose", "used_for_selection",
        "status", "selection_spec_hash", "previous_event_hash", "event_hash",
    }
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, required, path)
        seen: set[tuple[str, int]] = set()
        for line, row in enumerate(reader, 2):
            dataset_id = row["dataset_id"].strip()
            try:
                access_order = int(row["access_order"])
                event_time = datetime.fromisoformat(row["event_time"])
            except ValueError as exc:
                raise ContractError(f"{path}:{line} invalid access_order or event_time") from exc
            key = (dataset_id, access_order)
            if not dataset_id or access_order < 0 or key in seen:
                raise ContractError(f"{path}:{line} blank, negative, or duplicate ledger key")
            seen.add(key)
            rows.append({
                "dataset_id": dataset_id, "access_order": access_order, "event_time": event_time,
                "purpose": row["purpose"].strip(),
                "used_for_selection": parse_bool(row["used_for_selection"], f"{path}:{line}:used_for_selection"),
                "status": row["status"].strip(), "selection_spec_hash": row["selection_spec_hash"].strip(),
                "previous_event_hash": row["previous_event_hash"].strip(),
                "event_hash": row["event_hash"].strip(),
            })
    ordered = sorted(rows, key=lambda row: (row["dataset_id"], row["access_order"]))
    previous_by_dataset: dict[str, str] = {}
    for row in ordered:
        expected_previous = previous_by_dataset.get(row["dataset_id"], "")
        if row["previous_event_hash"] != expected_previous:
            raise ContractError(f"ledger chain break for dataset {row['dataset_id']}")
        payload = {
            "dataset_id": row["dataset_id"], "access_order": row["access_order"],
            "event_time": row["event_time"].isoformat(), "purpose": row["purpose"],
            "used_for_selection": row["used_for_selection"], "status": row["status"],
            "selection_spec_hash": row["selection_spec_hash"],
            "previous_event_hash": row["previous_event_hash"],
        }
        expected_hash = object_hash(payload)
        if row["event_hash"] != expected_hash:
            raise ContractError(f"ledger event hash mismatch for dataset {row['dataset_id']}")
        previous_by_dataset[row["dataset_id"]] = expected_hash
    return ordered


def load_selection_spec(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read selection spec {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("datasets"), dict):
        raise ContractError("selection spec requires a datasets object")
    if not str(value.get("family_id", "")).strip() or not str(value.get("frozen_at", "")).strip():
        raise ContractError("selection spec requires family_id and frozen_at")
    try:
        datetime.fromisoformat(value["frozen_at"])
    except ValueError as exc:
        raise ContractError("selection spec frozen_at must be ISO datetime") from exc
    return value


def read_signals(path: Path | None) -> dict[str, dict[date, set[str]]]:
    if path is None:
        return {}
    result: dict[str, dict[date, set[str]]] = defaultdict(lambda: defaultdict(set))
    seen: set[tuple[date, str, str]] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, {"date", "candidate_id", "asset_id"}, path)
        for line, row in enumerate(reader, 2):
            day = parse_date(row["date"], f"{path}:{line}:date")
            candidate = row["candidate_id"].strip()
            asset = row["asset_id"].strip()
            key = (day, candidate, asset)
            if not candidate or not asset or key in seen:
                raise ContractError(f"{path}:{line} blank or duplicate signal row")
            seen.add(key)
            result[candidate][day].add(asset)
    return {candidate: dict(days) for candidate, days in result.items()}


def load_study(path: Path) -> dict[str, Any]:
    try:
        study = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read study JSON {path}: {exc}") from exc
    for field in ("family_id", "annualization", "quality_top_k", "n_values", "hac_lags", "deployments"):
        if field not in study:
            raise ContractError(f"study missing {field}")
    if int(study["annualization"]) <= 0 or int(study["quality_top_k"]) <= 0:
        raise ContractError("annualization and quality_top_k must be positive")
    if int(study["hac_lags"]) < 0:
        raise ContractError("hac_lags must be non-negative")
    n_values = study["n_values"]
    if not isinstance(n_values, list) or not n_values or any(int(n) <= 0 for n in n_values):
        raise ContractError("n_values must be a non-empty list of positive integers")
    if len({int(n) for n in n_values}) != len(n_values):
        raise ContractError("n_values contains duplicates")
    if "decision_rules" in study:
        required_rules = {
            "minimum_return_retention", "minimum_sharpe_delta", "maximum_drawdown_worsening",
            "minimum_ordinary_beta_reduction", "minimum_downside_beta_reduction",
            "minimum_tail10_beta_reduction", "minimum_residual_alpha",
            "minimum_config_pass_rate",
        }
        missing_rules = sorted(required_rules - set(study["decision_rules"]))
        if missing_rules:
            raise ContractError(f"decision_rules missing: {', '.join(missing_rules)}")
        for key in required_rules:
            finite_float(study["decision_rules"][key], f"decision_rules.{key}")
        pass_rate = float(study["decision_rules"]["minimum_config_pass_rate"])
        if not 0 <= pass_rate <= 1:
            raise ContractError("minimum_config_pass_rate must be between zero and one")
        if float(study["decision_rules"]["maximum_drawdown_worsening"]) < 0:
            raise ContractError("maximum_drawdown_worsening must be non-negative")
    seen_ids: set[str] = set()
    seen_dataset_ids: set[str] = set()
    if not str(study["family_id"]).strip():
        raise ContractError("family_id must be non-empty")
    allowed_roles = {"development", "independent_holdout", "consumed_research", "forward_monitoring"}
    for deployment in study["deployments"]:
        deployment_id = str(deployment.get("deployment_id", "")).strip()
        if not deployment_id or deployment_id in seen_ids:
            raise ContractError("deployment_id must be non-empty and unique")
        seen_ids.add(deployment_id)
        est_start = parse_date(deployment["estimation"]["start"], "estimation.start")
        est_end = parse_date(deployment["estimation"]["end"], "estimation.end")
        eval_start = parse_date(deployment["evaluation"]["start"], "evaluation.start")
        eval_end = parse_date(deployment["evaluation"]["end"], "evaluation.end")
        if est_start > est_end or eval_start > eval_end or est_end >= eval_start:
            raise ContractError(f"deployment {deployment_id} has overlapping or reversed windows")
        role = deployment.get("evidence_role")
        consumed = deployment.get("consumed_for_selection")
        if role not in allowed_roles or not isinstance(consumed, bool):
            raise ContractError(f"deployment {deployment_id} has invalid evidence role")
        if consumed and role == "independent_holdout":
            raise ContractError(f"deployment {deployment_id} consumed evidence cannot be independent_holdout")
        if not str(deployment.get("benchmark_id", "")).strip():
            raise ContractError(f"deployment {deployment_id} missing benchmark_id")
        dataset_id = str(deployment.get("evaluation_dataset_id", "")).strip()
        if not dataset_id or dataset_id in seen_dataset_ids:
            raise ContractError(f"deployment {deployment_id} has missing or duplicate evaluation_dataset_id")
        seen_dataset_ids.add(dataset_id)
        if not str(deployment.get("evaluation_accessed_at", "")).strip():
            raise ContractError(f"deployment {deployment_id} missing evaluation_accessed_at")
        try:
            datetime.fromisoformat(deployment["evaluation_accessed_at"])
        except ValueError as exc:
            raise ContractError(f"deployment {deployment_id} has invalid evaluation_accessed_at") from exc
        if role in {"independent_holdout", "forward_monitoring"}:
            for field in ("selection_frozen_at", "evaluation_accessed_at", "selection_spec_hash"):
                if not str(deployment.get(field, "")).strip():
                    raise ContractError(f"deployment {deployment_id} missing {field}")
            if not is_sha256(str(deployment["selection_spec_hash"])):
                raise ContractError(f"deployment {deployment_id} selection_spec_hash must be SHA-256")
            try:
                frozen_at = datetime.fromisoformat(deployment["selection_frozen_at"])
                accessed_at = datetime.fromisoformat(deployment["evaluation_accessed_at"])
            except ValueError as exc:
                raise ContractError(f"deployment {deployment_id} has invalid freeze/access time") from exc
            if frozen_at >= accessed_at:
                raise ContractError(f"deployment {deployment_id} selection must freeze before evaluation access")
    return study


def window_dates(series: Mapping[date, float], start: date, end: date) -> list[date]:
    return sorted(day for day in series if start <= day <= end)


def ols_with_hac(
    y: Sequence[float], benchmark: Sequence[float], controls: Sequence[Sequence[float]], hac_lags: int
) -> dict[str, Any]:
    yv = np.asarray(y, dtype=float)
    bv = np.asarray(benchmark, dtype=float)
    if len(yv) < 3 or len(yv) != len(bv):
        raise ContractError("regression requires aligned observations and at least three rows")
    control_array = np.asarray(controls, dtype=float)
    if control_array.size == 0:
        control_array = np.empty((len(yv), 0))
    if control_array.ndim != 2 or control_array.shape[0] != len(yv):
        raise ContractError("control matrix is not aligned")
    x = np.column_stack((np.ones(len(yv)), bv, control_array))
    if np.linalg.matrix_rank(x) < x.shape[1]:
        raise ContractError("regression design matrix is rank deficient")
    coefficients, _, _, _ = np.linalg.lstsq(x, yv, rcond=None)
    residuals = yv - x @ coefficients
    xtx_inv = np.linalg.inv(x.T @ x)
    meat = np.zeros((x.shape[1], x.shape[1]))
    for t in range(len(yv)):
        meat += residuals[t] ** 2 * np.outer(x[t], x[t])
    max_lag = min(hac_lags, len(yv) - 1)
    for lag in range(1, max_lag + 1):
        weight = 1.0 - lag / (max_lag + 1.0)
        cross = np.zeros_like(meat)
        for t in range(lag, len(yv)):
            cross += residuals[t] * residuals[t - lag] * np.outer(x[t], x[t - lag])
        meat += weight * (cross + cross.T)
    covariance = xtx_inv @ meat @ xtx_inv
    alpha_se = math.sqrt(max(float(covariance[0, 0]), 0.0))
    return {
        "alpha": float(coefficients[0]),
        "beta": float(coefficients[1]),
        "alpha_hac_se": alpha_se,
        "residuals": residuals.tolist(),
    }


def simple_beta(y: Sequence[float], benchmark: Sequence[float]) -> float:
    if len(y) < 3:
        return math.nan
    bv = np.asarray(benchmark, dtype=float)
    variance = float(np.var(bv, ddof=1))
    if variance <= 0:
        return math.nan
    return float(np.cov(np.asarray(y, dtype=float), bv, ddof=1)[0, 1] / variance)


def risk_metrics(
    y: Sequence[float], benchmark: Sequence[float], controls: Sequence[Sequence[float]],
    annualization: int, hac_lags: int,
) -> tuple[dict[str, float], list[float]]:
    ordinary = ols_with_hac(y, benchmark, [], hac_lags)
    controlled = ols_with_hac(y, benchmark, controls, hac_lags)
    downside_idx = [i for i, value in enumerate(benchmark) if value < 0]
    order = sorted(range(len(benchmark)), key=lambda i: (benchmark[i], i))

    def conditional_beta(indices: Sequence[int]) -> float:
        return simple_beta([y[i] for i in indices], [benchmark[i] for i in indices])

    tail10_count = math.ceil(len(order) * 0.10)
    tail5_count = math.ceil(len(order) * 0.05)
    loss_days = downside_idx
    metrics = {
        "ordinary_beta": ordinary["beta"],
        "controlled_beta": controlled["beta"],
        "downside_beta": conditional_beta(downside_idx),
        "tail_10_beta": conditional_beta(order[:tail10_count]),
        "tail_5_beta": conditional_beta(order[:tail5_count]),
        "common_loss_rate": (
            sum(1 for i in loss_days if y[i] < 0) / len(loss_days) if loss_days else math.nan
        ),
        "residual_alpha": controlled["alpha"] * annualization,
        "residual_alpha_hac_se": controlled["alpha_hac_se"] * annualization,
        "observations": float(len(y)),
    }
    return metrics, controlled["residuals"]


def performance_metrics(returns: Sequence[float], annualization: int) -> dict[str, float]:
    if not returns:
        return {"annual_return": math.nan, "sharpe": math.nan, "max_drawdown": math.nan}
    values = np.asarray(returns, dtype=float)
    wealth = np.cumprod(1.0 + values)
    if np.any(wealth <= 0):
        annual_return = -1.0
    else:
        annual_return = float(wealth[-1] ** (annualization / len(values)) - 1.0)
    volatility = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    sharpe = float(np.mean(values) / volatility * math.sqrt(annualization)) if volatility > 0 else math.nan
    peaks = np.maximum.accumulate(np.concatenate(([1.0], wealth)))
    path = np.concatenate(([1.0], wealth))
    max_drawdown = float(np.min(path / peaks - 1.0))
    return {"annual_return": annual_return, "sharpe": sharpe, "max_drawdown": max_drawdown}


def compound_return(returns: Sequence[float]) -> float:
    result = 1.0
    for value in returns:
        result *= 1.0 + value
    return result - 1.0


def percentile_scores(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> dict[str, float]:
    scores: dict[str, list[float]] = defaultdict(list)
    for key in keys:
        values = sorted((float(row[key]), str(row["candidate_id"])) for row in rows)
        if any(not math.isfinite(value) for value, _ in values):
            raise ContractError(f"cannot rank non-finite {key}")
        n = len(values)
        start = 0
        while start < n:
            end = start + 1
            while end < n and values[end][0] == values[start][0]:
                end += 1
            percentile = ((start + end - 1) / 2) / max(n - 1, 1)
            for _, candidate in values[start:end]:
                scores[candidate].append(percentile)
            start = end
    return {candidate: max(parts) for candidate, parts in scores.items()}


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2 or len(a) != len(b):
        return math.nan
    av = np.asarray(a, dtype=float)
    bv = np.asarray(b, dtype=float)
    if float(np.std(av)) == 0 or float(np.std(bv)) == 0:
        return math.nan
    return float(np.corrcoef(av, bv)[0, 1])


def pairwise_values(members: Sequence[str], series: Mapping[str, Sequence[float]]) -> list[float]:
    values: list[float] = []
    for first, second in itertools.combinations(sorted(members), 2):
        value = pearson(series[first], series[second])
        if math.isfinite(value):
            values.append(value)
    return values


def median(values: Sequence[float]) -> float:
    return float(np.median(np.asarray(values, dtype=float))) if values else math.nan


def mean_signal_jaccard(
    first: str, second: str, signals: Mapping[str, Mapping[date, set[str]]], start: date, end: date
) -> float:
    if first not in signals or second not in signals:
        return math.nan
    days = sorted(
        day for day in set(signals[first]) & set(signals[second]) if start <= day <= end
    )
    if not days:
        return math.nan
    values = []
    for day in days:
        union = signals[first][day] | signals[second][day]
        values.append(len(signals[first][day] & signals[second][day]) / len(union) if union else 1.0)
    return sum(values) / len(values)


def portfolio_series(members: Sequence[str], panel: Mapping[str, Mapping[date, float]], days: Sequence[date]) -> list[float]:
    if not members:
        raise ContractError("portfolio has no members")
    missing = [(candidate, day) for candidate in members for day in days if day not in panel[candidate]]
    if missing:
        candidate, day = missing[0]
        raise ContractError(f"candidate {candidate} missing return on {day.isoformat()}")
    weight = 1.0 / len(members)
    return [sum(panel[candidate][day] * weight for candidate in members) for day in days]


def select_fixed_schemes(quality_pool: Sequence[Mapping[str, Any]], n: int) -> dict[str, list[str]]:
    if len(quality_pool) < n:
        return {}
    quality = sorted(quality_pool, key=lambda row: (-float(row["quality_score"]), str(row["candidate_id"])))
    low_beta = sorted(quality_pool, key=lambda row: (float(row["ordinary_beta"]), str(row["candidate_id"])))
    robust_scores = percentile_scores(quality_pool, RISK_KEYS)
    robust = sorted(quality_pool, key=lambda row: (robust_scores[str(row["candidate_id"])], str(row["candidate_id"])))
    return {
        "QUALITY_EQ": [str(row["candidate_id"]) for row in quality[:n]],
        "LOW_BETA_EQ": [str(row["candidate_id"]) for row in low_beta[:n]],
        "ROBUST_BETA_EQ": [str(row["candidate_id"]) for row in robust[:n]],
    }


def enumerate_decorrelated(
    pool: Sequence[Mapping[str, Any]], n: int, residuals: Mapping[str, Sequence[float]],
    beta_caps: Mapping[str, float], max_combinations: int,
    clusters: Mapping[str, str] | None = None, minimum_clusters: int | None = None,
    max_per_cluster: int | None = None,
) -> tuple[list[str] | None, str | None, int]:
    candidates = sorted(str(row["candidate_id"]) for row in pool)
    lookup = {str(row["candidate_id"]): row for row in pool}
    count = math.comb(len(candidates), n) if len(candidates) >= n else 0
    if count == 0:
        return None, "insufficient_candidates", count
    if count > max_combinations:
        return None, "combination_limit_exceeded", count
    best: tuple[Any, ...] | None = None
    best_members: list[str] | None = None
    for combo in itertools.combinations(candidates, n):
        if clusters is not None:
            labels = [clusters.get(candidate) for candidate in combo]
            if any(label is None for label in labels):
                continue
            counts = Counter(labels)
            if minimum_clusters is not None and len(counts) < minimum_clusters:
                continue
            if max_per_cluster is not None and max(counts.values()) > max_per_cluster:
                continue
        combo_betas = {
            key: sum(float(lookup[candidate][key]) for candidate in combo) / n for key in RISK_KEYS
        }
        if any(combo_betas[key] > float(beta_caps[key]) + 1e-12 for key in RISK_KEYS):
            continue
        correlations = pairwise_values(combo, residuals)
        if len(correlations) != n * (n - 1) // 2:
            continue
        objective = (
            median(correlations), max(correlations), combo_betas["tail_10_beta"],
            combo_betas["ordinary_beta"], combo,
        )
        if best is None or objective < best:
            best = objective
            best_members = list(combo)
    if best_members is None:
        return None, "no_feasible_combination", count
    return best_members, None, count


def json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else row.get(field, "") for field in fieldnames})


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
