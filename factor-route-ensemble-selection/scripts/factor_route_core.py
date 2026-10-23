#!/usr/bin/env python3
"""Deterministic core for structural factor routes and rolling N-selection."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
ALLOWED_AXES = {
    "fdr_q",
    "eligibility_status",
    "dedup_method",
    "correlation_aggregation",
    "linkage",
    "correlation_threshold",
    "distance_threshold",
    "representative_rule",
}
EVIDENCE_ROLES = {"development_consumed", "diagnostic_consumed", "independent_forward"}
BUILTIN_METRICS = {"sharpe", "calmar", "positive_month_rate"}


class ContractError(ValueError):
    """Raised when a frozen research contract is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_date(raw: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be ISO YYYY-MM-DD, got {raw!r}") from exc


def ensure_empty_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ContractError(f"Output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read valid JSON {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_csv_rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ContractError(f"{path} is missing columns: {', '.join(sorted(missing))}")
        return [dict(row) for row in reader]


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ContractError("Grid values must be finite")
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def expand_axis(name: str, spec: Any) -> list[Any]:
    if name not in ALLOWED_AXES:
        raise ContractError(
            f"structural_search axis {name!r} is forbidden or unknown; allowed={sorted(ALLOWED_AXES)}"
        )
    if not isinstance(spec, dict):
        raise ContractError(f"Axis {name!r} must be an object")
    if "values" in spec:
        values = spec["values"]
        if not isinstance(values, list) or not values:
            raise ContractError(f"Axis {name!r}.values must be a non-empty list")
        normalized: list[Any] = []
        for value in values:
            if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
                raise ContractError(f"Axis {name!r} has unsupported value {value!r}")
            if isinstance(value, (int, float)):
                try:
                    normalized.append(_decimal_text(Decimal(str(value))))
                except InvalidOperation as exc:
                    raise ContractError(f"Axis {name!r} contains invalid number") from exc
            else:
                text = str(value).strip()
                if not text:
                    raise ContractError(f"Axis {name!r} contains a blank value")
                normalized.append(text)
        if len({json.dumps(v, sort_keys=True) for v in normalized}) != len(normalized):
            raise ContractError(f"Axis {name!r} contains duplicate values")
        return normalized
    if not {"start", "end", "step"} <= set(spec):
        raise ContractError(f"Axis {name!r} requires values or start/end/step")
    try:
        start = Decimal(str(spec["start"]))
        end = Decimal(str(spec["end"]))
        step = Decimal(str(spec["step"]))
    except InvalidOperation as exc:
        raise ContractError(f"Axis {name!r} range is not valid decimal data") from exc
    if not all(value.is_finite() for value in (start, end, step)) or step <= 0 or start > end:
        raise ContractError(f"Axis {name!r} requires finite start <= end and step > 0")
    values: list[Any] = []
    current = start
    while current <= end:
        values.append(_decimal_text(current))
        current += step
        if len(values) > 1_000_000:
            raise ContractError(f"Axis {name!r} expands beyond safety limit")
    if Decimal(values[-1]) != end:
        raise ContractError(f"Axis {name!r} step does not land exactly on end")
    return values


def _ema20_axes() -> dict[str, Any]:
    return {
        "fdr_q": {"start": "0.05", "end": "0.50", "step": "0.005"},
        "correlation_threshold": {"start": "0.30", "end": "0.90", "step": "0.025"},
        "linkage": {"values": ["average"]},
    }


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("Study config must be a JSON object")
    config = json.loads(json.dumps(raw))
    study_id = config.get("study_id")
    if not isinstance(study_id, str) or not study_id.strip():
        raise ContractError("study_id must be a non-empty string")
    if not isinstance(config.get("user_confirmed"), bool):
        raise ContractError("user_confirmed must be explicitly true or false")

    upstream = config.get("upstream")
    upstream_fields = {
        "factor_evidence_hash",
        "label_spec_hash",
        "preprocessing_hash",
        "model_spec_hash",
        "account_spec_hash",
        "data_hash",
        "code_hash",
    }
    if not isinstance(upstream, dict):
        raise ContractError("upstream must be an object")
    for field in upstream_fields:
        if not isinstance(upstream.get(field), str) or not upstream[field].strip():
            raise ContractError(f"upstream.{field} must be a non-empty string")

    periods = config.get("deployment_periods")
    if not isinstance(periods, list) or not periods:
        raise ContractError("deployment_periods must be a non-empty list")
    seen_ids: set[str] = set()
    previous_end: date | None = None
    normalized_periods: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        if not isinstance(period, dict):
            raise ContractError(f"deployment_periods[{index}] must be an object")
        period_id = str(period.get("id", "")).strip()
        if not period_id or period_id in seen_ids:
            raise ContractError("deployment period ids must be non-empty and unique")
        seen_ids.add(period_id)
        cutoff = parse_date(period.get("information_cutoff"), f"{period_id}.information_cutoff")
        selection_start = parse_date(period.get("selection_start"), f"{period_id}.selection_start")
        selection_end = parse_date(period.get("selection_end"), f"{period_id}.selection_end")
        deployment_start = parse_date(period.get("deployment_start"), f"{period_id}.deployment_start")
        deployment_end = parse_date(period.get("deployment_end"), f"{period_id}.deployment_end")
        if not selection_start <= cutoff <= selection_end < deployment_start <= deployment_end:
            raise ContractError(
                f"{period_id} must satisfy selection_start <= information_cutoff <= "
                "selection_end < deployment_start <= deployment_end"
            )
        if previous_end is not None and deployment_start <= previous_end:
            raise ContractError("deployment periods must be ordered and non-overlapping")
        previous_end = deployment_end
        role = period.get("evidence_role")
        if role not in EVIDENCE_ROLES:
            raise ContractError(f"{period_id}.evidence_role must be one of {sorted(EVIDENCE_ROLES)}")
        normalized_periods.append(
            {
                **period,
                "id": period_id,
                "information_cutoff": cutoff.isoformat(),
                "selection_start": selection_start.isoformat(),
                "selection_end": selection_end.isoformat(),
                "deployment_start": deployment_start.isoformat(),
                "deployment_end": deployment_end.isoformat(),
            }
        )
    config["deployment_periods"] = normalized_periods

    search = config.get("structural_search")
    if not isinstance(search, dict):
        raise ContractError("structural_search must be an object")
    profile = search.get("profile", "custom")
    if profile not in {"custom", "ema20_reference", "confirmed_ema20_reference"}:
        raise ContractError("structural_search.profile is invalid")
    if profile == "ema20_reference" and config["user_confirmed"]:
        raise ContractError("ema20_reference is advisory; use confirmed_ema20_reference after confirmation")
    axes = search.get("axes")
    if axes is None and profile in {"ema20_reference", "confirmed_ema20_reference"}:
        axes = _ema20_axes()
    if not isinstance(axes, dict) or not axes:
        raise ContractError("structural_search.axes must be a non-empty object")
    expanded_axes = {name: expand_axis(name, spec) for name, spec in sorted(axes.items())}
    search["axes"] = axes
    search["expanded_axes"] = expanded_axes
    config["structural_search"] = search

    candidate_count = math.prod(len(values) for values in expanded_axes.values())
    if profile in {"ema20_reference", "confirmed_ema20_reference"}:
        config.setdefault("compression", {"target_medoids": 1024, "nested_sizes": [64, 128, 256, 512, 1024]})
        config.setdefault(
            "fixed_ensembles",
            {
                "route_equal_weight": 0.5,
                "factor_set_equal_weight": 0.5,
                "diagnostics": ["candidate_mass", "parameter_bin_equal", "median", "trimmed_mean", "consensus_penalty"],
                "trim_fraction": 0.1,
                "consensus_penalty": 0.25,
                "parameter_bins": {
                    "axes": ["fdr_q", "correlation_threshold"],
                    "counts": [10, 10],
                },
            },
        )
        config.setdefault(
            "selection",
            {
                "lookback_complete_calendar_years": 3,
                "metrics": [
                    {"name": "sharpe", "direction": "max"},
                    {"name": "calmar", "direction": "max"},
                    {"name": "positive_month_rate", "direction": "max"},
                ],
                "n_values": [1, 3, 5, 10, 20, 100, 300, 1000],
                "matches": 100,
                "seed": 2026,
                "overlap_threshold": 0.6,
                "top_k": [3],
            },
        )
        config.setdefault("statistics", {"annualization": 252, "hac_lag": 5, "fdr_alpha": 0.1})
    compression = config.get("compression")
    if not isinstance(compression, dict):
        raise ContractError("compression must be an object")
    target = compression.get("target_medoids")
    if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= candidate_count:
        raise ContractError("compression.target_medoids must be within the candidate count")
    nested = compression.get("nested_sizes", [])
    if not isinstance(nested, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in nested
    ):
        raise ContractError("compression.nested_sizes must contain positive integers")
    if nested != sorted(set(nested)) or any(value > target for value in nested):
        raise ContractError("nested_sizes must be unique, increasing, and <= target_medoids")

    model = config.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("seeds"), list) or not model["seeds"]:
        raise ContractError("model.seeds must be a non-empty list")
    if any(not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in model["seeds"]):
        raise ContractError("model.seeds must be non-negative integers")
    if len(set(model["seeds"])) != len(model["seeds"]):
        raise ContractError("model.seeds must be unique")

    fixed = config.get("fixed_ensembles")
    if not isinstance(fixed, dict):
        raise ContractError("fixed_ensembles must be an object")
    route_weight = fixed.get("route_equal_weight")
    factor_weight = fixed.get("factor_set_equal_weight")
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 for value in (route_weight, factor_weight)):
        raise ContractError("fixed ensemble weights must be non-negative numbers")
    if not math.isclose(float(route_weight) + float(factor_weight), 1.0, abs_tol=1e-12):
        raise ContractError("fixed ensemble weights must sum to 1")
    diagnostics = fixed.get("diagnostics", [])
    allowed_diagnostics = {
        "candidate_mass",
        "parameter_bin_equal",
        "median",
        "trimmed_mean",
        "consensus_penalty",
    }
    if not isinstance(diagnostics, list) or not set(diagnostics) <= allowed_diagnostics:
        raise ContractError(f"fixed_ensembles.diagnostics must be within {sorted(allowed_diagnostics)}")
    if "parameter_bin_equal" in diagnostics:
        bin_spec = fixed.get("parameter_bins")
        if not isinstance(bin_spec, dict):
            raise ContractError("parameter_bin_equal requires fixed_ensembles.parameter_bins")
        bin_axes = bin_spec.get("axes")
        bin_counts = bin_spec.get("counts")
        if (
            not isinstance(bin_axes, list)
            or len(bin_axes) != 2
            or any(axis not in expanded_axes for axis in bin_axes)
            or not isinstance(bin_counts, list)
            or len(bin_counts) != 2
            or any(not isinstance(count, int) or count <= 0 for count in bin_counts)
        ):
            raise ContractError("parameter_bins requires two searched axes and two positive bin counts")
        for axis in bin_axes:
            try:
                [Decimal(str(value)) for value in expanded_axes[axis]]
            except InvalidOperation as exc:
                raise ContractError("parameter_bin_equal axes must contain numeric values") from exc
    trim_fraction = float(fixed.get("trim_fraction", 0.1))
    penalty = float(fixed.get("consensus_penalty", 0.25))
    if not 0 <= trim_fraction < 0.5 or penalty < 0:
        raise ContractError("trim_fraction must be in [0,0.5); consensus_penalty must be >= 0")

    selection = config.get("selection")
    if not isinstance(selection, dict):
        raise ContractError("selection must be an object")
    lookback = selection.get("lookback_complete_calendar_years")
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback <= 0:
        raise ContractError("selection.lookback_complete_calendar_years must be positive")
    for period in normalized_periods:
        ds = parse_date(period["deployment_start"], "deployment_start")
        ss = parse_date(period["selection_start"], "selection_start")
        se = parse_date(period["selection_end"], "selection_end")
        if ds.month != 1 or ds.day != 1 or ss != date(ds.year - lookback, 1, 1) or se != date(ds.year - 1, 12, 31):
            raise ContractError(
                f"{period['id']} selection window must be the prior {lookback} complete calendar years"
            )
    metrics = selection.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ContractError("selection.metrics must be a non-empty list")
    metric_names: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str):
            raise ContractError("Each selection metric needs name and direction")
        name = metric["name"].strip()
        if not name or name in metric_names or metric.get("direction") not in {"max", "min"}:
            raise ContractError("Selection metric names must be unique and directions max|min")
        metric_names.add(name)
        if name not in BUILTIN_METRICS and not metric.get("adapter_provided", False):
            raise ContractError(f"Custom metric {name!r} must declare adapter_provided=true")
    n_values = selection.get("n_values")
    if not isinstance(n_values, list) or not n_values or any(
        not isinstance(n, int) or isinstance(n, bool) or n <= 0 or n > target for n in n_values
    ):
        raise ContractError("selection.n_values must be positive, unique, and <= target_medoids")
    if len(set(n_values)) != len(n_values):
        raise ContractError("selection.n_values contains duplicates")
    if not isinstance(selection.get("matches"), int) or selection["matches"] <= 0:
        raise ContractError("selection.matches must be positive")
    if not isinstance(selection.get("seed"), int) or selection["seed"] < 0:
        raise ContractError("selection.seed must be a non-negative integer")
    threshold = selection.get("overlap_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= threshold <= 1:
        raise ContractError("selection.overlap_threshold must be in [0,1]")
    top_k = selection.get("top_k", [])
    if not isinstance(top_k, list) or any(not isinstance(k, int) or k <= 0 for k in top_k):
        raise ContractError("selection.top_k must contain positive integers")

    statistics = config.get("statistics")
    if not isinstance(statistics, dict):
        raise ContractError("statistics must be an object")
    annualization = statistics.get("annualization")
    hac_lag = statistics.get("hac_lag")
    alpha = statistics.get("fdr_alpha")
    if not isinstance(annualization, int) or annualization <= 0:
        raise ContractError("statistics.annualization must be positive")
    if not isinstance(hac_lag, int) or hac_lag < 0:
        raise ContractError("statistics.hac_lag must be non-negative")
    if not isinstance(alpha, (int, float)) or not 0 < alpha < 1:
        raise ContractError("statistics.fdr_alpha must be in (0,1)")
    return config


def build_candidates(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    axes = config["structural_search"]["expanded_axes"]
    names = sorted(axes)
    rows: list[dict[str, Any]] = []
    for values in itertools.product(*(axes[name] for name in names)):
        params = dict(zip(names, values))
        spec_hash = stable_hash(params)
        rows.append(
            {
                "candidate_id": f"route-{spec_hash[:16]}",
                "route_spec_hash": spec_hash,
                "params": params,
            }
        )
    return sorted(rows, key=lambda row: row["candidate_id"])


def load_frozen_plan(plan_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec_path = plan_dir / "frozen_spec.json"
    candidates_path = plan_dir / "candidate_grid.jsonl"
    if not spec_path.exists() or not candidates_path.exists():
        raise ContractError("Plan is not frozen or is missing candidate_grid.jsonl")
    spec = read_json(spec_path)
    manifest = read_json(plan_dir / "input_manifest.json")
    if manifest.get("frozen_spec_sha256") != sha256_file(spec_path):
        raise ContractError("Frozen spec hash does not match input_manifest.json")
    if manifest.get("candidate_grid_sha256") != sha256_file(candidates_path):
        raise ContractError("Candidate grid hash does not match input_manifest.json")
    candidates: list[dict[str, Any]] = []
    for line_number, line in enumerate(candidates_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ContractError(f"Invalid candidate grid line {line_number}") from exc
    return spec, candidates


@dataclass(frozen=True)
class LineageData:
    factor_sets: dict[tuple[str, str], tuple[str, ...]]
    factor_hashes: dict[tuple[str, str], str]
    complete_candidates: tuple[str, ...]
    failures: tuple[dict[str, Any], ...]


def validate_lineage(
    path: Path, config: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> LineageData:
    required = {
        "candidate_id",
        "deployment_id",
        "information_cutoff",
        "factor_id",
        "factor_order",
        "factor_set_hash",
        "route_status",
        "source_evidence_hash",
        "route_spec_hash",
    }
    rows = read_csv_rows(path, required)
    candidate_map = {row["candidate_id"]: row for row in candidates}
    period_map = {row["id"]: row for row in config["deployment_periods"]}
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    unknown: list[str] = []
    for row in rows:
        key = (row["candidate_id"].strip(), row["deployment_id"].strip())
        if key[0] not in candidate_map or key[1] not in period_map:
            unknown.append("/".join(key))
            continue
        grouped[key].append(row)
    if unknown:
        raise ContractError(f"Lineage contains unplanned candidate/deployment keys: {unknown[:5]}")

    factor_sets: dict[tuple[str, str], tuple[str, ...]] = {}
    factor_hashes: dict[tuple[str, str], str] = {}
    failures: list[dict[str, Any]] = []
    complete: list[str] = []
    for candidate_id in sorted(candidate_map):
        candidate_ok = True
        for deployment_id, period in sorted(period_map.items()):
            key = (candidate_id, deployment_id)
            group = grouped.get(key, [])
            reason = ""
            if not group:
                reason = "missing_lineage"
            elif any(row["route_status"] != "completed" for row in group):
                reason = "route_not_completed"
            elif any(row["source_evidence_hash"] != config["upstream"]["factor_evidence_hash"] for row in group):
                reason = "source_evidence_hash_mismatch"
            elif any(row["route_spec_hash"] != candidate_map[candidate_id]["route_spec_hash"] for row in group):
                reason = "route_spec_hash_mismatch"
            elif any(row["information_cutoff"] != period["information_cutoff"] for row in group):
                reason = "information_cutoff_mismatch"
            else:
                try:
                    indexed = sorted((int(row["factor_order"]), row["factor_id"].strip()) for row in group)
                except ValueError:
                    indexed = []
                    reason = "invalid_factor_order"
                if not reason:
                    orders = [item[0] for item in indexed]
                    factors = [item[1] for item in indexed]
                    if not factors or any(not factor for factor in factors):
                        reason = "empty_factor_set"
                    elif orders != list(range(1, len(orders) + 1)):
                        reason = "non_contiguous_factor_order"
                    elif len(set(factors)) != len(factors):
                        reason = "duplicate_factor"
                    else:
                        expected_hash = stable_hash(factors)
                        supplied = {row["factor_set_hash"] for row in group}
                        if supplied != {expected_hash}:
                            reason = "factor_set_hash_mismatch"
                        else:
                            factor_sets[key] = tuple(factors)
                            factor_hashes[key] = expected_hash
            if reason:
                candidate_ok = False
                failures.append(
                    {
                        "stage": "lineage",
                        "candidate_id": candidate_id,
                        "deployment_id": deployment_id,
                        "reason": reason,
                    }
                )
        if candidate_ok:
            complete.append(candidate_id)
    if len(complete) < config["compression"]["target_medoids"]:
        raise ContractError(
            "Complete route count is smaller than compression.target_medoids; failures remain in denominator"
        )
    return LineageData(factor_sets, factor_hashes, tuple(complete), tuple(failures))


def route_distance_matrix(
    lineage: LineageData, deployment_ids: Sequence[str]
) -> tuple[list[str], Any]:
    try:
        import numpy as np
        from scipy.sparse import csr_matrix
    except ImportError as exc:
        raise ContractError("NumPy and SciPy are required; install scripts/requirements.txt") from exc
    ids = list(lineage.complete_candidates)
    matrix = np.zeros((len(ids), len(ids)), dtype=np.float64)
    for deployment_id in deployment_ids:
        universe = sorted(
            {
                factor_id
                for candidate_id in ids
                for factor_id in lineage.factor_sets[(candidate_id, deployment_id)]
            }
        )
        positions = {factor_id: index for index, factor_id in enumerate(universe)}
        row_indexes: list[int] = []
        column_indexes: list[int] = []
        factor_counts: list[int] = []
        for row_index, candidate_id in enumerate(ids):
            factors = lineage.factor_sets[(candidate_id, deployment_id)]
            if not factors:
                raise ContractError("Empty factor sets cannot enter the route distance matrix")
            factor_counts.append(len(factors))
            for factor_id in factors:
                row_indexes.append(row_index)
                column_indexes.append(positions[factor_id])
        membership = csr_matrix(
            (np.ones(len(row_indexes), dtype=np.int32), (row_indexes, column_indexes)),
            shape=(len(ids), len(universe)),
        )
        intersections = (membership @ membership.T).toarray().astype(np.float64, copy=False)
        counts = np.asarray(factor_counts, dtype=np.float64)
        unions = counts[:, None] + counts[None, :] - intersections
        if np.any(unions <= 0):
            raise ContractError("Empty factor sets cannot enter the route distance matrix")
        matrix += 1.0 - intersections / unions
    matrix /= len(deployment_ids)
    np.fill_diagonal(matrix, 0.0)
    return ids, matrix


def cluster_routes(
    candidate_ids: Sequence[str], distance_matrix: Any, target_clusters: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import numpy as np
        from scipy.cluster.hierarchy import cut_tree, linkage
        from scipy.spatial.distance import squareform
    except ImportError as exc:
        raise ContractError("NumPy and SciPy are required; install scripts/requirements.txt") from exc
    if not 1 <= target_clusters <= len(candidate_ids):
        raise ContractError("target_clusters is outside candidate count")
    if len(candidate_ids) == 1:
        labels = np.array([0], dtype=int)
    else:
        condensed = squareform(distance_matrix, checks=True)
        tree = linkage(condensed, method="average", optimal_ordering=False)
        labels = cut_tree(tree, n_clusters=[target_clusters]).reshape(-1)
    members_by_label: dict[int, list[int]] = defaultdict(list)
    for index, label in enumerate(labels.tolist()):
        members_by_label[int(label)].append(index)

    cluster_records: list[tuple[str, int, int, float]] = []
    medoid_rows: list[dict[str, Any]] = []
    for label, indexes in members_by_label.items():
        candidates: list[tuple[float, str, int]] = []
        for index in indexes:
            avg = float(distance_matrix[index, indexes].mean())
            candidates.append((avg, candidate_ids[index], index))
        best_average = min(item[0] for item in candidates)
        medoid_id = min(item[1] for item in candidates if math.isclose(item[0], best_average, abs_tol=1e-14))
        medoid_index = candidate_ids.index(medoid_id)
        cluster_records.append((medoid_id, label, medoid_index, best_average))
    cluster_records.sort()
    label_to_cluster = {label: position for position, (_, label, _, _) in enumerate(cluster_records, 1)}
    mapping: list[dict[str, Any]] = []
    for medoid_id, label, medoid_index, best_average in cluster_records:
        indexes = members_by_label[label]
        cluster_id = label_to_cluster[label]
        medoid_rows.append(
            {
                "cluster_id": cluster_id,
                "medoid_candidate_id": medoid_id,
                "represented_candidate_count": len(indexes),
                "mean_within_cluster_distance": format(best_average, ".17g"),
            }
        )
        for index in sorted(indexes, key=lambda value: candidate_ids[value]):
            mapping.append(
                {
                    "candidate_id": candidate_ids[index],
                    "cluster_id": cluster_id,
                    "medoid_candidate_id": medoid_id,
                    "distance_to_medoid": format(float(distance_matrix[index, medoid_index]), ".17g"),
                    "represented_candidate_count": len(indexes),
                }
            )
    return medoid_rows, sorted(mapping, key=lambda row: row["candidate_id"])


def nested_farthest_points(
    medoid_ids: Sequence[str], all_candidate_ids: Sequence[str], distance_matrix: Any, sizes: Sequence[int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not sizes:
        return [], []
    index = {candidate_id: position for position, candidate_id in enumerate(all_candidate_ids)}
    ordered_medoids = sorted(medoid_ids)
    selected = [ordered_medoids[0]]
    remaining = set(ordered_medoids[1:])
    while remaining:
        scored = []
        for candidate_id in remaining:
            nearest = min(float(distance_matrix[index[candidate_id], index[kept]]) for kept in selected)
            scored.append((nearest, candidate_id))
        best_distance = max(value for value, _ in scored)
        chosen = min(candidate_id for value, candidate_id in scored if math.isclose(value, best_distance, abs_tol=1e-14))
        selected.append(chosen)
        remaining.remove(chosen)
    membership: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for size in sizes:
        subset = selected[:size]
        membership.extend(
            {"route_set_id": f"L{size}", "position": position, "candidate_id": candidate_id}
            for position, candidate_id in enumerate(subset, 1)
        )
        nearest_values = [
            min(float(distance_matrix[index[candidate_id], index[kept]]) for kept in subset)
            for candidate_id in all_candidate_ids
        ]
        sorted_values = sorted(nearest_values)
        p95_index = max(0, math.ceil(0.95 * len(sorted_values)) - 1)
        coverage.append(
            {
                "route_set_id": f"L{size}",
                "route_count": size,
                "mean_nearest_distance": format(mean(nearest_values), ".17g"),
                "p95_nearest_distance": format(sorted_values[p95_index], ".17g"),
                "max_nearest_distance": format(max(nearest_values), ".17g"),
            }
        )
    return membership, coverage


def build_model_reuse(
    config: Mapping[str, Any], lineage: LineageData, medoid_ids: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    upstream = config["upstream"]
    unique: dict[str, dict[str, Any]] = {}
    reuse: list[dict[str, Any]] = []
    for deployment in config["deployment_periods"]:
        deployment_id = deployment["id"]
        for candidate_id in sorted(medoid_ids):
            factor_hash = lineage.factor_hashes[(candidate_id, deployment_id)]
            for seed in config["model"]["seeds"]:
                identity = {
                    "deployment_id": deployment_id,
                    "ordered_factor_set_hash": factor_hash,
                    "preprocessing_hash": upstream["preprocessing_hash"],
                    "model_spec_hash": upstream["model_spec_hash"],
                    "seed": seed,
                    "data_hash": upstream["data_hash"],
                    "code_hash": upstream["code_hash"],
                }
                model_trial_id = f"model-{stable_hash(identity)[:20]}"
                unique.setdefault(model_trial_id, {"model_trial_id": model_trial_id, **identity})
                reuse.append(
                    {
                        "deployment_id": deployment_id,
                        "candidate_id": candidate_id,
                        "seed": seed,
                        "model_trial_id": model_trial_id,
                        "ordered_factor_set_hash": factor_hash,
                    }
                )
    unique_rows = sorted(unique.values(), key=lambda row: row["model_trial_id"])
    plan_rows = [{**row, "status": "planned"} for row in unique_rows]
    return unique_rows, sorted(reuse, key=lambda row: (row["deployment_id"], row["candidate_id"], row["seed"])), plan_rows


def derived_seed(root_seed: int, deployment_id: str, n: int) -> int:
    digest = stable_hash({"seed": root_seed, "deployment_id": deployment_id, "n": n})
    return int(digest[:16], 16)


def build_tournament_groups(
    candidate_ids: Sequence[str], deployment_ids: Sequence[str], n_values: Sequence[int], matches: int, seed: int
) -> list[dict[str, Any]]:
    pool = sorted(candidate_ids)
    rows: list[dict[str, Any]] = []
    for deployment_id in deployment_ids:
        for n in n_values:
            if n > len(pool):
                raise ContractError(f"N={n} exceeds candidate count {len(pool)}")
            rng = random.Random(derived_seed(seed, deployment_id, n))
            for match in range(1, matches + 1):
                for position, candidate_id in enumerate(rng.sample(pool, n), 1):
                    rows.append(
                        {
                            "deployment_id": deployment_id,
                            "n": n,
                            "match": match,
                            "position": position,
                            "candidate_id": candidate_id,
                        }
                    )
    return rows


def compute_metric(values_by_date: Sequence[tuple[date, float]], metric: str, annualization: int) -> float:
    values = [value for _, value in values_by_date]
    if not values:
        return math.nan
    if metric == "sharpe":
        return mean(values) / stdev(values) * math.sqrt(annualization) if len(values) >= 2 and stdev(values) > 0 else math.nan
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1.0)
    if metric == "calmar":
        if wealth <= 0 or max_drawdown == 0:
            return math.nan
        annual_return = wealth ** (annualization / len(values)) - 1.0
        return annual_return / abs(max_drawdown)
    if metric == "positive_month_rate":
        monthly: dict[tuple[int, int], float] = defaultdict(lambda: 1.0)
        for day, value in values_by_date:
            monthly[(day.year, day.month)] *= 1.0 + value
        return sum(1 for wealth_factor in monthly.values() if wealth_factor - 1.0 > 0) / len(monthly)
    raise ContractError(f"Metric {metric!r} must be adapter-provided")


def signal_similarity(
    left: Mapping[date, frozenset[str]], right: Mapping[date, frozenset[str]]
) -> float:
    if set(left) != set(right) or not left:
        raise ContractError("Members must share the same non-empty rebalance calendar")
    daily: list[float] = []
    for day in sorted(left):
        if not left[day] or len(left[day]) != len(right[day]):
            raise ContractError(f"Members must select the same positive asset count on {day}")
        daily.append(len(left[day] & right[day]) / len(left[day]))
    return mean(daily)


def bh_qvalues(pairs: Sequence[tuple[int, float]]) -> dict[int, float]:
    ordered = sorted(pairs, key=lambda item: (item[1], item[0]))
    m = len(ordered)
    result: dict[int, float] = {}
    running = 1.0
    for rank in range(m, 0, -1):
        row_id, p_value = ordered[rank - 1]
        running = min(running, p_value * m / rank)
        result[row_id] = min(1.0, running)
    return result


def account_metrics(values: Sequence[float], annualization: int) -> dict[str, float]:
    if not values:
        raise ContractError("Account metric input is empty")
    wealth = 1.0
    peak = 1.0
    mdd = 0.0
    for value in values:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        mdd = min(mdd, wealth / peak - 1.0)
    annual_return = wealth ** (annualization / len(values)) - 1.0 if wealth > 0 else -1.0
    sigma = stdev(values) if len(values) >= 2 else 0.0
    sharpe = mean(values) / sigma * math.sqrt(annualization) if sigma > 0 else math.nan
    calmar = annual_return / abs(mdd) if mdd < 0 else math.nan
    return {
        "cumulative_return": wealth - 1.0,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": calmar,
    }


def hac_mean_test(values: Sequence[float], lag: int) -> tuple[float, float, float]:
    n = len(values)
    if n < 2:
        return mean(values) if values else math.nan, math.nan, math.nan
    avg = mean(values)
    centered = [value - avg for value in values]
    long_run = sum(value * value for value in centered) / n
    for current_lag in range(1, min(lag, n - 1) + 1):
        covariance = sum(centered[index] * centered[index - current_lag] for index in range(current_lag, n)) / n
        long_run += 2.0 * (1.0 - current_lag / (lag + 1.0)) * covariance
    variance_mean = max(long_run / n, 0.0)
    if variance_mean == 0:
        return avg, math.nan, math.nan
    t_stat = avg / math.sqrt(variance_mean)
    p_value = math.erfc(abs(t_stat) / math.sqrt(2.0))
    return avg, t_stat, p_value
