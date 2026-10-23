#!/usr/bin/env python3
"""Run structural compression, fixed ensembles, rolling N-selection, and summaries."""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from factor_route_core import (
    ContractError,
    SCHEMA_VERSION,
    account_metrics,
    bh_qvalues,
    build_model_reuse,
    build_tournament_groups,
    cluster_routes,
    compute_metric,
    ensure_empty_dir,
    hac_mean_test,
    load_frozen_plan,
    nested_farthest_points,
    parse_date,
    read_csv_rows,
    read_json,
    route_distance_matrix,
    sha256_file,
    signal_similarity,
    stable_hash,
    validate_lineage,
    write_csv,
    write_json,
    write_jsonl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    structure = subparsers.add_parser("structure", help="Blind structural route compression")
    structure.add_argument("--plan-dir", required=True, type=Path)
    structure.add_argument("--lineage", required=True, type=Path)
    structure.add_argument("--output-dir", required=True, type=Path)

    aggregate = subparsers.add_parser("aggregate", help="Build fixed prediction ensembles")
    aggregate.add_argument("--plan-dir", required=True, type=Path)
    aggregate.add_argument("--structure-dir", required=True, type=Path)
    aggregate.add_argument("--predictions", required=True, type=Path)
    aggregate.add_argument("--output-dir", required=True, type=Path)

    select = subparsers.add_parser("select", help="Run annual rolling tournament N-selection")
    select.add_argument("--plan-dir", required=True, type=Path)
    select.add_argument("--structure-dir", required=True, type=Path)
    select.add_argument("--member-returns", required=True, type=Path)
    select.add_argument("--member-signals", required=True, type=Path)
    select.add_argument("--custom-scores", type=Path)
    select.add_argument("--output-dir", required=True, type=Path)

    summarize = subparsers.add_parser("summarize", help="Compare formal accounts and build report")
    summarize.add_argument("--plan-dir", required=True, type=Path)
    summarize.add_argument("--fixed-accounts", required=True, type=Path)
    summarize.add_argument("--selected-accounts", required=True, type=Path)
    summarize.add_argument("--output-dir", required=True, type=Path)
    return parser


def _artifact_hashes(directory: Path, filenames: Sequence[str]) -> dict[str, str]:
    return {filename: sha256_file(directory / filename) for filename in filenames}


def _load_structure(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "structure_manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("stage") != "structure_complete":
        raise ContractError("Unsupported structure manifest")
    expected = manifest.get("structure_hash")
    content = {key: value for key, value in manifest.items() if key != "structure_hash"}
    if expected != stable_hash(content):
        raise ContractError("structure_manifest.json hash is invalid")
    for filename, digest in manifest.get("artifacts", {}).items():
        path = directory / filename
        if not path.exists() or sha256_file(path) != digest:
            raise ContractError(f"Frozen structure artifact changed: {path}")
    return manifest


def _cmd_structure(args: argparse.Namespace) -> None:
    try:
        import numpy as np
        import scipy  # noqa: F401
    except ImportError as exc:
        raise ContractError("NumPy and SciPy are required; install scripts/requirements.txt") from exc
    config, candidates = load_frozen_plan(args.plan_dir)
    lineage = validate_lineage(args.lineage, config, candidates)
    deployment_ids = [period["id"] for period in config["deployment_periods"]]
    candidate_ids, matrix = route_distance_matrix(lineage, deployment_ids)
    medoids, mapping = cluster_routes(
        candidate_ids, matrix, config["compression"]["target_medoids"]
    )
    medoid_ids = [row["medoid_candidate_id"] for row in medoids]
    nested, coverage = nested_farthest_points(
        medoid_ids,
        candidate_ids,
        matrix,
        config["compression"].get("nested_sizes", []),
    )
    unique_models, reuse_map, model_plan = build_model_reuse(config, lineage, medoid_ids)
    lineage_diagnostics: list[dict[str, Any]] = []
    for deployment_id in deployment_ids:
        hashes = [lineage.factor_hashes[(candidate_id, deployment_id)] for candidate_id in medoid_ids]
        counts = Counter(hashes)
        probabilities = [count / len(hashes) for count in counts.values()]
        entropy = -sum(value * math.log(value) for value in probabilities)
        factor_counts = [len(lineage.factor_sets[(candidate_id, deployment_id)]) for candidate_id in medoid_ids]
        factor_union = {
            factor_id
            for candidate_id in medoid_ids
            for factor_id in lineage.factor_sets[(candidate_id, deployment_id)]
        }
        lineage_diagnostics.append(
            {
                "deployment_id": deployment_id,
                "route_count": len(medoid_ids),
                "unique_factor_sets": len(counts),
                "effective_factor_sets": format(math.exp(entropy), ".17g"),
                "largest_factor_set_share": format(max(probabilities), ".17g"),
                "median_factor_count": format(float(median(factor_counts)), ".17g"),
                "mean_factor_count": format(sum(factor_counts) / len(factor_counts), ".17g"),
                "distinct_factors": len(factor_union),
            }
        )

    ensure_empty_dir(args.output_dir)
    shutil.copyfile(args.lineage, args.output_dir / "route_factor_lineage.csv")
    np.savez_compressed(
        args.output_dir / "route_distance.npz",
        candidate_ids=np.asarray(candidate_ids, dtype=str),
        distance=matrix,
    )
    write_csv(
        args.output_dir / "medoids.csv",
        ["cluster_id", "medoid_candidate_id", "represented_candidate_count", "mean_within_cluster_distance"],
        medoids,
    )
    write_csv(
        args.output_dir / "candidate_to_medoid.csv",
        ["candidate_id", "cluster_id", "medoid_candidate_id", "distance_to_medoid", "represented_candidate_count"],
        mapping,
    )
    cluster_rows = [
        {
            "cluster_id": row["cluster_id"],
            "medoid_candidate_id": row["medoid_candidate_id"],
            "member_candidate_id": row["candidate_id"],
            "distance_to_medoid": row["distance_to_medoid"],
        }
        for row in mapping
    ]
    write_csv(
        args.output_dir / "route_clusters.csv",
        ["cluster_id", "medoid_candidate_id", "member_candidate_id", "distance_to_medoid"],
        cluster_rows,
    )
    write_csv(
        args.output_dir / "nested_route_sets.csv",
        ["route_set_id", "position", "candidate_id"],
        nested,
    )
    write_csv(
        args.output_dir / "nested_coverage.csv",
        ["route_set_id", "route_count", "mean_nearest_distance", "p95_nearest_distance", "max_nearest_distance"],
        coverage,
    )
    unique_fields = [
        "model_trial_id",
        "deployment_id",
        "ordered_factor_set_hash",
        "preprocessing_hash",
        "model_spec_hash",
        "seed",
        "data_hash",
        "code_hash",
    ]
    write_csv(args.output_dir / "unique_annual_models.csv", unique_fields, unique_models)
    write_csv(
        args.output_dir / "model_reuse_map.csv",
        ["deployment_id", "candidate_id", "seed", "model_trial_id", "ordered_factor_set_hash"],
        reuse_map,
    )
    write_jsonl(args.output_dir / "model_trial_plan.jsonl", model_plan)
    write_csv(
        args.output_dir / "training_completeness.csv",
        ["model_trial_id", "status"],
        ({"model_trial_id": row["model_trial_id"], "status": "planned"} for row in unique_models),
    )
    write_csv(
        args.output_dir / "failure_records.csv",
        ["stage", "candidate_id", "deployment_id", "reason"],
        lineage.failures,
    )
    write_csv(
        args.output_dir / "annual_lineage_diagnostics.csv",
        [
            "deployment_id",
            "route_count",
            "unique_factor_sets",
            "effective_factor_sets",
            "largest_factor_set_share",
            "median_factor_count",
            "mean_factor_count",
            "distinct_factors",
        ],
        lineage_diagnostics,
    )
    artifacts = [
        "route_factor_lineage.csv",
        "route_distance.npz",
        "medoids.csv",
        "candidate_to_medoid.csv",
        "route_clusters.csv",
        "nested_route_sets.csv",
        "nested_coverage.csv",
        "unique_annual_models.csv",
        "model_reuse_map.csv",
        "model_trial_plan.jsonl",
        "training_completeness.csv",
        "failure_records.csv",
        "annual_lineage_diagnostics.csv",
    ]
    base = {
        "schema_version": SCHEMA_VERSION,
        "stage": "structure_complete",
        "plan_hash": config["plan_hash"],
        "lineage_input_sha256": sha256_file(args.lineage),
        "candidate_denominator": len(candidates),
        "complete_route_count": len(candidate_ids),
        "failed_route_period_rows": len(lineage.failures),
        "medoid_count": len(medoids),
        "unique_model_trial_count": len(unique_models),
        "artifacts": _artifact_hashes(args.output_dir, artifacts),
    }
    manifest = {**base, "structure_hash": stable_hash(base)}
    write_json(args.output_dir / "structure_manifest.json", manifest)
    write_json(
        args.output_dir / "SUCCESS.json",
        {"status": "complete_with_recorded_failures" if lineage.failures else "complete", "structure_hash": manifest["structure_hash"]},
    )


def _load_frame(path: Path) -> Any:
    try:
        import pandas as pd
    except ImportError as exc:
        raise ContractError("pandas is required; install scripts/requirements.txt") from exc
    try:
        return pd.read_parquet(path) if path.suffix.lower() in {".parquet", ".pq"} else pd.read_csv(path)
    except Exception as exc:
        raise ContractError(f"Cannot read tabular input {path}: {exc}") from exc


def _cmd_aggregate(args: argparse.Namespace) -> None:
    try:
        import numpy as np
        import pandas as pd
    except ImportError as exc:
        raise ContractError("NumPy and pandas are required; install scripts/requirements.txt") from exc
    config, candidates = load_frozen_plan(args.plan_dir)
    structure_manifest = _load_structure(args.structure_dir)
    if structure_manifest["plan_hash"] != config["plan_hash"]:
        raise ContractError("Structure and plan hashes do not match")
    medoid_rows = read_csv_rows(args.structure_dir / "medoids.csv", {"medoid_candidate_id", "represented_candidate_count"})
    medoid_ids = sorted(row["medoid_candidate_id"] for row in medoid_rows)
    mass = {row["medoid_candidate_id"]: int(row["represented_candidate_count"]) for row in medoid_rows}
    candidate_params = {row["candidate_id"]: row["params"] for row in candidates}
    nested_rows = read_csv_rows(
        args.structure_dir / "nested_route_sets.csv", {"route_set_id", "position", "candidate_id"}
    )
    nested_members: dict[str, list[str]] = defaultdict(list)
    for row in nested_rows:
        nested_members[row["route_set_id"]].append(row["candidate_id"])
    lineage_rows = read_csv_rows(
        args.structure_dir / "route_factor_lineage.csv",
        {"candidate_id", "deployment_id", "factor_set_hash"},
    )
    factor_hashes = {
        (row["candidate_id"], row["deployment_id"]): row["factor_set_hash"]
        for row in lineage_rows
        if row["candidate_id"] in set(medoid_ids)
    }

    frame = _load_frame(args.predictions)
    required = {"deployment_id", "decision_time", "instrument_id", "candidate_id", "score", "prediction_hash"}
    missing = required - set(frame.columns)
    if missing:
        raise ContractError(f"Predictions are missing columns: {sorted(missing)}")
    if frame[list(required - {"score"})].isna().any().any():
        raise ContractError("Predictions contain blank identity fields")
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    if not np.isfinite(frame["score"].to_numpy()).all():
        raise ContractError("Predictions contain non-finite scores")
    duplicate = frame.duplicated(["deployment_id", "decision_time", "instrument_id", "candidate_id"])
    if duplicate.any():
        raise ContractError("Predictions contain duplicate deployment/decision/instrument/candidate keys")
    planned_deployments = {period["id"] for period in config["deployment_periods"]}
    if set(frame["deployment_id"].astype(str)) != planned_deployments:
        raise ContractError("Predictions do not cover exactly the planned deployment ids")
    if set(frame["candidate_id"].astype(str)) != set(medoid_ids):
        raise ContractError("Predictions must contain exactly the formal medoid members")
    frame["deployment_id"] = frame["deployment_id"].astype(str)
    frame["candidate_id"] = frame["candidate_id"].astype(str)
    frame["member_rank"] = frame.groupby(
        ["deployment_id", "decision_time", "candidate_id"], sort=False
    )["score"].rank(method="average", pct=True)

    diagnostics = set(config["fixed_ensembles"].get("diagnostics", []))
    weight_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    weighted_deployments: set[str] = set()
    for (deployment_id, decision_time), group in frame.groupby(["deployment_id", "decision_time"], sort=True):
        pivot = group.pivot(index="instrument_id", columns="candidate_id", values="member_rank")
        if set(pivot.columns) != set(medoid_ids) or pivot.isna().any().any():
            raise ContractError(f"Incomplete member/instrument denominator at {deployment_id}/{decision_time}")
        pivot = pivot[medoid_ids]
        route_weights = {candidate_id: 1.0 / len(medoid_ids) for candidate_id in medoid_ids}
        sets_by_hash: dict[str, list[str]] = defaultdict(list)
        for candidate_id in medoid_ids:
            try:
                sets_by_hash[factor_hashes[(candidate_id, deployment_id)]].append(candidate_id)
            except KeyError as exc:
                raise ContractError(f"Missing factor-set lineage for {candidate_id}/{deployment_id}") from exc
        factor_weights = {
            candidate_id: 1.0 / len(sets_by_hash) / len(members)
            for members in sets_by_hash.values()
            for candidate_id in members
        }
        blend_weights = {
            candidate_id: (
                config["fixed_ensembles"]["route_equal_weight"] * route_weights[candidate_id]
                + config["fixed_ensembles"]["factor_set_equal_weight"] * factor_weights[candidate_id]
            )
            for candidate_id in medoid_ids
        }
        weight_sets: dict[str, dict[str, float]] = {
            "route_equal": route_weights,
            "annual_factor_set_equal": factor_weights,
            "fixed_blend": blend_weights,
        }
        for route_set_id, members in sorted(nested_members.items()):
            if not set(members) <= set(medoid_ids):
                raise ContractError(f"Nested route set {route_set_id} contains a non-medoid member")
            weight_sets[f"route_equal_{route_set_id}"] = {
                candidate_id: 1.0 / len(members) for candidate_id in members
            }
        if "candidate_mass" in diagnostics:
            total_mass = sum(mass.values())
            weight_sets["candidate_mass"] = {candidate_id: mass[candidate_id] / total_mass for candidate_id in medoid_ids}
        if "parameter_bin_equal" in diagnostics:
            bin_spec = config["fixed_ensembles"]["parameter_bins"]
            bin_axes = bin_spec["axes"]
            bin_counts = bin_spec["counts"]
            axis_ranges = []
            for axis in bin_axes:
                values = [float(value) for value in config["structural_search"]["expanded_axes"][axis]]
                axis_ranges.append((min(values), max(values)))
            routes_by_bin: dict[tuple[int, int], list[str]] = defaultdict(list)
            for candidate_id in medoid_ids:
                indexes = []
                for axis, count, (lower, upper) in zip(bin_axes, bin_counts, axis_ranges):
                    value = float(candidate_params[candidate_id][axis])
                    if math.isclose(lower, upper):
                        index = 0
                    else:
                        index = min(count - 1, int((value - lower) / (upper - lower) * count))
                    indexes.append(index)
                routes_by_bin[(indexes[0], indexes[1])].append(candidate_id)
            weight_sets["parameter_bin_equal"] = {
                candidate_id: 1.0 / len(routes_by_bin) / len(members)
                for members in routes_by_bin.values()
                for candidate_id in members
            }
        if deployment_id not in weighted_deployments:
            for aggregator_id, weights in weight_sets.items():
                for candidate_id, weight in sorted(weights.items()):
                    weight_rows.append(
                        {
                            "deployment_id": deployment_id,
                            "aggregator_id": aggregator_id,
                            "candidate_id": candidate_id,
                            "weight": format(weight, ".17g"),
                            "factor_set_hash": factor_hashes[(candidate_id, deployment_id)],
                        }
                    )
            weighted_deployments.add(deployment_id)
        aggregator_scores: dict[str, Any] = {
            aggregator_id: pivot[list(weights)].dot(pd.Series(weights))
            for aggregator_id, weights in weight_sets.items()
        }
        if "median" in diagnostics:
            aggregator_scores["median"] = pivot.median(axis=1)
        if "trimmed_mean" in diagnostics:
            values = np.sort(pivot.to_numpy(), axis=1)
            trim = int(math.floor(values.shape[1] * float(config["fixed_ensembles"].get("trim_fraction", 0.1))))
            selected = values[:, trim : values.shape[1] - trim] if trim else values
            aggregator_scores["trimmed_mean"] = pd.Series(selected.mean(axis=1), index=pivot.index)
        if "consensus_penalty" in diagnostics:
            penalty = float(config["fixed_ensembles"].get("consensus_penalty", 0.25))
            aggregator_scores["consensus_penalty"] = pivot.mean(axis=1) - penalty * pivot.std(axis=1, ddof=0)
        for aggregator_id, scores in aggregator_scores.items():
            for instrument_id, score in scores.items():
                output_rows.append(
                    {
                        "aggregator_id": aggregator_id,
                        "deployment_id": deployment_id,
                        "decision_time": decision_time,
                        "instrument_id": instrument_id,
                        "score": float(score),
                    }
                )

    ensure_empty_dir(args.output_dir)
    write_csv(
        args.output_dir / "fixed_ensemble_weights.csv",
        ["deployment_id", "aggregator_id", "candidate_id", "weight", "factor_set_hash"],
        sorted(weight_rows, key=lambda row: (row["deployment_id"], row["aggregator_id"], row["candidate_id"])),
    )
    weight_diagnostics: list[dict[str, Any]] = []
    grouped_weights: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in weight_rows:
        grouped_weights[(row["deployment_id"], row["aggregator_id"])].append(row)
    for (deployment_id, aggregator_id), rows in sorted(grouped_weights.items()):
        weights = [float(row["weight"]) for row in rows]
        weight_diagnostics.append(
            {
                "deployment_id": deployment_id,
                "aggregator_id": aggregator_id,
                "member_count": len(rows),
                "unique_factor_sets": len({row["factor_set_hash"] for row in rows}),
                "maximum_member_weight": format(max(weights), ".17g"),
                "effective_member_count": format(1.0 / sum(value * value for value in weights), ".17g"),
            }
        )
    write_csv(
        args.output_dir / "fixed_ensemble_diagnostics.csv",
        ["deployment_id", "aggregator_id", "member_count", "unique_factor_sets", "maximum_member_weight", "effective_member_count"],
        weight_diagnostics,
    )
    output_frame = pd.DataFrame(output_rows).sort_values(
        ["aggregator_id", "deployment_id", "decision_time", "instrument_id"]
    )
    try:
        output_frame.to_parquet(args.output_dir / "fixed_ensemble_predictions.parquet", index=False)
    except Exception as exc:
        raise ContractError(f"Cannot write Parquet; install pyarrow from scripts/requirements.txt: {exc}") from exc
    account_manifest = {
        "stage": "formal_account_pending",
        "continuous_across_deployments": True,
        "prediction_composition": "within-decision percentile ranks",
        "required_baselines": ["route_equal", "annual_factor_set_equal", "fixed_blend"],
        "account_spec_hash": config["upstream"]["account_spec_hash"],
        "prediction_file_sha256": sha256_file(args.output_dir / "fixed_ensemble_predictions.parquet"),
    }
    write_json(args.output_dir / "account_run_manifest.json", account_manifest)
    artifacts = ["fixed_ensemble_weights.csv", "fixed_ensemble_diagnostics.csv", "fixed_ensemble_predictions.parquet", "account_run_manifest.json"]
    base = {
        "schema_version": SCHEMA_VERSION,
        "stage": "fixed_ensembles_complete",
        "plan_hash": config["plan_hash"],
        "structure_hash": structure_manifest["structure_hash"],
        "prediction_input_sha256": sha256_file(args.predictions),
        "artifacts": _artifact_hashes(args.output_dir, artifacts),
    }
    manifest = {**base, "ensemble_hash": stable_hash(base)}
    write_json(args.output_dir / "ensemble_manifest.json", manifest)
    write_json(args.output_dir / "SUCCESS.json", {"status": "prediction_complete_account_pending", "ensemble_hash": manifest["ensemble_hash"]})


def _read_member_returns(path: Path, members: set[str]) -> dict[str, list[tuple[date, float]]]:
    rows = read_csv_rows(path, {"date", "candidate_id", "net_return"})
    result: dict[str, list[tuple[date, float]]] = defaultdict(list)
    seen: set[tuple[str, date]] = set()
    for row in rows:
        candidate_id = row["candidate_id"].strip()
        if candidate_id not in members:
            continue
        day = parse_date(row["date"], "member_returns.date")
        key = (candidate_id, day)
        if key in seen:
            raise ContractError(f"Duplicate member return key {candidate_id}/{day}")
        seen.add(key)
        try:
            value = float(row["net_return"])
        except ValueError as exc:
            raise ContractError("member_returns contains non-numeric net_return") from exc
        if not math.isfinite(value):
            raise ContractError("member_returns contains non-finite net_return")
        result[candidate_id].append((day, value))
    return {candidate_id: sorted(values) for candidate_id, values in result.items()}


def _read_signals(path: Path, members: set[str]) -> dict[str, dict[date, frozenset[str]]]:
    rows = read_csv_rows(path, {"rebalance_date", "candidate_id", "asset_id"})
    raw: dict[str, dict[date, set[str]]] = defaultdict(lambda: defaultdict(set))
    seen: set[tuple[str, date, str]] = set()
    for row in rows:
        candidate_id = row["candidate_id"].strip()
        if candidate_id not in members:
            continue
        day = parse_date(row["rebalance_date"], "member_signals.rebalance_date")
        asset_id = row["asset_id"].strip()
        if not asset_id:
            raise ContractError("member_signals contains blank asset_id")
        key = (candidate_id, day, asset_id)
        if key in seen:
            raise ContractError(f"Duplicate member signal key {key}")
        seen.add(key)
        raw[candidate_id][day].add(asset_id)
    return {
        candidate_id: {day: frozenset(assets) for day, assets in sorted(days.items())}
        for candidate_id, days in sorted(raw.items())
    }


def _custom_score_map(path: Path | None) -> dict[tuple[str, str, str], float]:
    if path is None:
        return {}
    rows = read_csv_rows(path, {"deployment_id", "candidate_id", "metric", "score"})
    result: dict[tuple[str, str, str], float] = {}
    for row in rows:
        key = (row["deployment_id"], row["candidate_id"], row["metric"])
        if key in result:
            raise ContractError(f"Duplicate custom score key {key}")
        try:
            value = float(row["score"])
        except ValueError as exc:
            raise ContractError("custom score is non-numeric") from exc
        result[key] = value
    return result


def _cmd_select(args: argparse.Namespace) -> None:
    config, _ = load_frozen_plan(args.plan_dir)
    structure_manifest = _load_structure(args.structure_dir)
    if structure_manifest["plan_hash"] != config["plan_hash"]:
        raise ContractError("Structure and plan hashes do not match")
    medoid_rows = read_csv_rows(args.structure_dir / "medoids.csv", {"medoid_candidate_id"})
    medoids = sorted(row["medoid_candidate_id"] for row in medoid_rows)
    member_set = set(medoids)
    returns = _read_member_returns(args.member_returns, member_set)
    signals = _read_signals(args.member_signals, member_set)
    if set(returns) != member_set or set(signals) != member_set:
        raise ContractError("Returns and signals must cover every formal medoid member")
    custom_scores = _custom_score_map(args.custom_scores)
    deployment_ids = [period["id"] for period in config["deployment_periods"]]
    groups = build_tournament_groups(
        medoids,
        deployment_ids,
        config["selection"]["n_values"],
        config["selection"]["matches"],
        config["selection"]["seed"],
    )
    groups_by_key: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    for row in groups:
        groups_by_key[(row["deployment_id"], row["n"], row["match"])].append(row["candidate_id"])

    score_rows: list[dict[str, Any]] = []
    winners: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    member_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    annual_summary: list[dict[str, Any]] = []
    annualization = config["statistics"]["annualization"]

    for period in config["deployment_periods"]:
        deployment_id = period["id"]
        selection_start = parse_date(period["selection_start"], "selection_start")
        selection_end = parse_date(period["selection_end"], "selection_end")
        deployment_start = parse_date(period["deployment_start"], "deployment_start")
        deployment_end = parse_date(period["deployment_end"], "deployment_end")
        window_returns: dict[str, list[tuple[date, float]]] = {
            candidate_id: [(day, value) for day, value in returns[candidate_id] if selection_start <= day <= selection_end]
            for candidate_id in medoids
        }
        calendars = {candidate_id: tuple(day for day, _ in values) for candidate_id, values in window_returns.items()}
        if any(not days for days in calendars.values()) or len(set(calendars.values())) != 1:
            raise ContractError(f"Member return calendars differ or are empty in {deployment_id} selection window")
        selection_signals = {
            candidate_id: {day: assets for day, assets in signals[candidate_id].items() if selection_start <= day <= selection_end}
            for candidate_id in medoids
        }
        reference_calendar = set(selection_signals[medoids[0]])
        if not reference_calendar:
            raise ContractError(f"Selection signal calendar is empty for {deployment_id}")
        for candidate_id in medoids:
            if set(selection_signals[candidate_id]) != reference_calendar:
                raise ContractError(f"Selection signal calendars differ in {deployment_id}")
        for day in reference_calendar:
            sizes = {len(selection_signals[candidate_id][day]) for candidate_id in medoids}
            if len(sizes) != 1 or next(iter(sizes)) <= 0:
                raise ContractError(f"Members must select the same positive K on {day}")

        scores_by_metric: dict[str, dict[str, float]] = {}
        for metric_spec in config["selection"]["metrics"]:
            metric = metric_spec["name"]
            metric_scores: dict[str, float] = {}
            for candidate_id in medoids:
                if metric in {"sharpe", "calmar", "positive_month_rate"}:
                    score = compute_metric(window_returns[candidate_id], metric, annualization)
                else:
                    score = custom_scores.get((deployment_id, candidate_id, metric), math.nan)
                metric_scores[candidate_id] = score
                score_rows.append(
                    {
                        "deployment_id": deployment_id,
                        "selection_start": period["selection_start"],
                        "selection_end": period["selection_end"],
                        "candidate_id": candidate_id,
                        "metric": metric,
                        "direction": metric_spec["direction"],
                        "score": "" if not math.isfinite(score) else format(score, ".17g"),
                    }
                )
            scores_by_metric[metric] = metric_scores

        deployment_signals = {
            candidate_id: {day: assets for day, assets in signals[candidate_id].items() if deployment_start <= day <= deployment_end}
            for candidate_id in medoids
        }
        deployment_calendar = sorted({day for days in deployment_signals.values() for day in days})
        if not deployment_calendar:
            raise ContractError(f"Deployment signal calendar is empty for {deployment_id}")

        for metric_spec in config["selection"]["metrics"]:
            metric = metric_spec["name"]
            direction = metric_spec["direction"]
            scores = scores_by_metric[metric]
            for n in config["selection"]["n_values"]:
                counts: Counter[str] = Counter()
                for match in range(1, config["selection"]["matches"] + 1):
                    group = groups_by_key[(deployment_id, n, match)]
                    valid = [candidate_id for candidate_id in group if math.isfinite(scores[candidate_id])]
                    if not valid:
                        winners.append(
                            {"deployment_id": deployment_id, "metric": metric, "n": n, "match": match, "status": "invalid_group", "winner_candidate_id": "", "winner_score": ""}
                        )
                        continue
                    best = (max if direction == "max" else min)(scores[candidate_id] for candidate_id in valid)
                    winner = min(candidate_id for candidate_id in valid if scores[candidate_id] == best)
                    counts[winner] += 1
                    winners.append(
                        {"deployment_id": deployment_id, "metric": metric, "n": n, "match": match, "status": "winner", "winner_candidate_id": winner, "winner_score": format(best, ".17g")}
                    )
                if not counts:
                    failures.append({"stage": "selection", "deployment_id": deployment_id, "metric": metric, "n": n, "reason": "no_valid_winners"})
                    continue
                ordered = sorted(
                    counts,
                    key=lambda candidate_id: (
                        -counts[candidate_id],
                        -scores[candidate_id] if direction == "max" else scores[candidate_id],
                        candidate_id,
                    ),
                )
                kept: list[str] = []
                for order, candidate_id in enumerate(ordered, 1):
                    comparisons = [
                        (signal_similarity(selection_signals[candidate_id], selection_signals[retained]), retained)
                        for retained in kept
                    ]
                    blockers = [item for item in comparisons if item[0] > config["selection"]["overlap_threshold"]]
                    if blockers:
                        max_similarity = max(value for value, _ in blockers)
                        blocker = min(retained for value, retained in blockers if math.isclose(value, max_similarity, abs_tol=1e-14))
                        decision = "removed"
                    else:
                        max_similarity = max((value for value, _ in comparisons), default=math.nan)
                        blocker = ""
                        decision = "kept"
                        kept.append(candidate_id)
                    traces.append(
                        {
                            "deployment_id": deployment_id,
                            "metric": metric,
                            "n": n,
                            "order": order,
                            "candidate_id": candidate_id,
                            "metric_score": format(scores[candidate_id], ".17g"),
                            "win_count": counts[candidate_id],
                            "max_similarity_to_kept": "" if not math.isfinite(max_similarity) else format(max_similarity, ".17g"),
                            "blocking_candidate_id": blocker,
                            "decision": decision,
                        }
                    )
                for candidate_id in kept:
                    member_rows.append(
                        {"deployment_id": deployment_id, "metric": metric, "n": n, "candidate_id": candidate_id, "metric_score": format(scores[candidate_id], ".17g"), "win_count": counts[candidate_id]}
                    )
                annual_summary.append(
                    {"deployment_id": deployment_id, "metric": metric, "n": n, "unique_winners": len(counts), "decorrelated_members": len(kept), "winning_slots": sum(counts.values()), "retained_slots": sum(counts[candidate_id] for candidate_id in kept)}
                )
                for day in deployment_calendar:
                    for vote_mode in ("slot_weighted", "unique_equal"):
                        votes: Counter[str] = Counter()
                        for candidate_id in kept:
                            assets = deployment_signals[candidate_id].get(day)
                            if not assets:
                                raise ContractError(f"Selected member {candidate_id} lacks deployment signal on {day}")
                            vote_weight = counts[candidate_id] if vote_mode == "slot_weighted" else 1
                            for asset_id in assets:
                                votes[asset_id] += vote_weight
                        ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
                        selections = [("all", None, ranked)] + [("top_k", k, ranked[:k]) for k in config["selection"].get("top_k", [])]
                        for mode, top_k, selected in selections:
                            total = sum(value for _, value in selected)
                            label = "all" if top_k is None else f"top_{top_k}"
                            configuration_id = f"{metric}__n{n}__{vote_mode}__{label}"
                            for asset_id, value in selected:
                                target_rows.append(
                                    {"deployment_id": deployment_id, "rebalance_date": day.isoformat(), "configuration_id": configuration_id, "metric": metric, "n": n, "member_vote_mode": vote_mode, "asset_selection_mode": mode, "top_k": "" if top_k is None else top_k, "asset_id": asset_id, "votes": value, "target_weight": format(value / total, ".17g")}
                                )

    ensure_empty_dir(args.output_dir)
    write_csv(args.output_dir / "annual_candidate_scores.csv", ["deployment_id", "selection_start", "selection_end", "candidate_id", "metric", "direction", "score"], score_rows)
    write_csv(args.output_dir / "tournament_groups.csv", ["deployment_id", "n", "match", "position", "candidate_id"], groups)
    write_csv(args.output_dir / "tournament_winners.csv", ["deployment_id", "metric", "n", "match", "status", "winner_candidate_id", "winner_score"], winners)
    write_csv(args.output_dir / "annual_selection_trace.csv", ["deployment_id", "metric", "n", "order", "candidate_id", "metric_score", "win_count", "max_similarity_to_kept", "blocking_candidate_id", "decision"], traces)
    write_csv(args.output_dir / "annual_decorrelated_members.csv", ["deployment_id", "metric", "n", "candidate_id", "metric_score", "win_count"], member_rows)
    write_csv(args.output_dir / "annual_selection_summary.csv", ["deployment_id", "metric", "n", "unique_winners", "decorrelated_members", "winning_slots", "retained_slots"], annual_summary)
    write_csv(args.output_dir / "target_weights.csv", ["deployment_id", "rebalance_date", "configuration_id", "metric", "n", "member_vote_mode", "asset_selection_mode", "top_k", "asset_id", "votes", "target_weight"], target_rows)
    write_csv(args.output_dir / "failure_records.csv", ["stage", "deployment_id", "metric", "n", "reason"], failures)
    account_manifest = {
        "stage": "formal_account_pending",
        "continuous_across_deployments": True,
        "annual_reset_forbidden": True,
        "account_spec_hash": config["upstream"]["account_spec_hash"],
        "target_weights_sha256": sha256_file(args.output_dir / "target_weights.csv"),
        "configuration_count": len({row["configuration_id"] for row in target_rows}),
    }
    write_json(args.output_dir / "account_run_manifest.json", account_manifest)
    artifacts = ["annual_candidate_scores.csv", "tournament_groups.csv", "tournament_winners.csv", "annual_selection_trace.csv", "annual_decorrelated_members.csv", "annual_selection_summary.csv", "target_weights.csv", "failure_records.csv", "account_run_manifest.json"]
    base = {
        "schema_version": SCHEMA_VERSION,
        "stage": "rolling_selection_complete",
        "plan_hash": config["plan_hash"],
        "structure_hash": structure_manifest["structure_hash"],
        "inputs": {
            "member_returns": sha256_file(args.member_returns),
            "member_signals": sha256_file(args.member_signals),
            "custom_scores": sha256_file(args.custom_scores) if args.custom_scores else None,
        },
        "artifacts": _artifact_hashes(args.output_dir, artifacts),
    }
    manifest = {**base, "selection_hash": stable_hash(base)}
    write_json(args.output_dir / "selection_manifest.json", manifest)
    write_json(args.output_dir / "SUCCESS.json", {"status": "targets_complete_account_pending", "selection_hash": manifest["selection_hash"]})


def _read_accounts(
    path: Path,
) -> tuple[dict[str, dict[date, float]], dict[str, dict[date, dict[str, float]]]]:
    rows = read_csv_rows(path, {"configuration_id", "date", "net_return", "continuous_account", "account_hash"})
    result: dict[str, dict[date, float]] = defaultdict(dict)
    auxiliary: dict[str, dict[date, dict[str, float]]] = defaultdict(dict)
    for row in rows:
        if row["continuous_account"].strip().lower() != "true":
            raise ContractError(f"Account {row['configuration_id']} is not marked continuous")
        configuration_id = row["configuration_id"].strip()
        day = parse_date(row["date"], "account.date")
        if day in result[configuration_id]:
            raise ContractError(f"Duplicate account date {configuration_id}/{day}")
        try:
            value = float(row["net_return"])
        except ValueError as exc:
            raise ContractError("Account net_return is non-numeric") from exc
        if not math.isfinite(value):
            raise ContractError("Account net_return is non-finite")
        if not row["account_hash"].strip():
            raise ContractError("Account hash is blank")
        result[configuration_id][day] = value
        parsed_aux: dict[str, float] = {}
        for field in ("turnover", "fill_rate", "cash_weight"):
            raw = (row.get(field) or "").strip()
            if raw:
                try:
                    parsed = float(raw)
                except ValueError as exc:
                    raise ContractError(f"Account {field} is non-numeric") from exc
                if not math.isfinite(parsed):
                    raise ContractError(f"Account {field} is non-finite")
                parsed_aux[field] = parsed
        auxiliary[configuration_id][day] = parsed_aux
    return dict(result), dict(auxiliary)


def _role_for_day(config: Mapping[str, Any], day: date) -> str | None:
    for period in config["deployment_periods"]:
        if parse_date(period["deployment_start"], "deployment_start") <= day <= parse_date(period["deployment_end"], "deployment_end"):
            return period["evidence_role"]
    return None


def _fmt(value: float) -> str:
    return "" if not math.isfinite(value) else format(value, ".17g")


def _cmd_summarize(args: argparse.Namespace) -> None:
    config, _ = load_frozen_plan(args.plan_dir)
    fixed, fixed_aux = _read_accounts(args.fixed_accounts)
    selected, selected_aux = _read_accounts(args.selected_accounts)
    required_baselines = {"route_equal", "annual_factor_set_equal", "fixed_blend"}
    if set(fixed) != required_baselines:
        raise ContractError(f"Fixed accounts must contain exactly {sorted(required_baselines)}")
    expected_selected = {
        f"{metric['name']}__n{n}__{vote_mode}__{label}"
        for metric in config["selection"]["metrics"]
        for n in config["selection"]["n_values"]
        for vote_mode in ("slot_weighted", "unique_equal")
        for label in (["all"] + [f"top_{k}" for k in config["selection"].get("top_k", [])])
    }
    if set(selected) != expected_selected:
        missing = sorted(expected_selected - set(selected))
        extra = sorted(set(selected) - expected_selected)
        raise ContractError(f"Selected account denominator mismatch; missing={missing[:5]}, extra={extra[:5]}")
    annualization = config["statistics"]["annualization"]
    roles = sorted({period["evidence_role"] for period in config["deployment_periods"]})
    all_accounts = {**fixed, **selected}
    all_auxiliary = {**fixed_aux, **selected_aux}
    metric_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    for configuration_id, returns in sorted(all_accounts.items()):
        metrics = account_metrics([returns[day] for day in sorted(returns)], annualization)
        auxiliary_metrics = {
            f"average_{field}": _fmt(sum(values) / len(values)) if values else ""
            for field in ("turnover", "fill_rate", "cash_weight")
            for values in [[details[field] for details in all_auxiliary[configuration_id].values() if field in details]]
        }
        metric_rows.append({"configuration_id": configuration_id, "account_type": "fixed" if configuration_id in fixed else "selected", **{key: _fmt(value) for key, value in metrics.items()}, **auxiliary_metrics})
        for role in roles:
            values = [returns[day] for day in sorted(returns) if _role_for_day(config, day) == role]
            if not values:
                continue
            role_metrics = account_metrics(values, annualization)
            role_days = {day for day in returns if _role_for_day(config, day) == role}
            role_auxiliary = {
                f"average_{field}": _fmt(sum(aux_values) / len(aux_values)) if aux_values else ""
                for field in ("turnover", "fill_rate", "cash_weight")
                for aux_values in [[details[field] for day, details in all_auxiliary[configuration_id].items() if day in role_days and field in details]]
            }
            period_rows.append({"configuration_id": configuration_id, "account_type": "fixed" if configuration_id in fixed else "selected", "evidence_period": role, **{key: _fmt(value) for key, value in role_metrics.items()}, **role_auxiliary})
        for period in config["deployment_periods"]:
            start = parse_date(period["deployment_start"], "deployment_start")
            end = parse_date(period["deployment_end"], "deployment_end")
            period_days = [day for day in sorted(returns) if start <= day <= end]
            if not period_days:
                continue
            deployment_metrics = account_metrics([returns[day] for day in period_days], annualization)
            annual_rows.append(
                {
                    "configuration_id": configuration_id,
                    "account_type": "fixed" if configuration_id in fixed else "selected",
                    "deployment_id": period["id"],
                    "evidence_period": period["evidence_role"],
                    **{key: _fmt(value) for key, value in deployment_metrics.items()},
                }
            )

    comparisons: list[dict[str, Any]] = []
    for baseline_id, baseline_returns in sorted(fixed.items()):
        for role in roles:
            baseline_days = {day for day in baseline_returns if _role_for_day(config, day) == role}
            for configuration_id, returns in sorted(selected.items()):
                selected_days = {day for day in returns if _role_for_day(config, day) == role}
                if selected_days != baseline_days or not selected_days:
                    raise ContractError(f"Paired return calendars differ for {configuration_id}/{baseline_id}/{role}")
                differences = [returns[day] - baseline_returns[day] for day in sorted(selected_days)]
                avg, t_stat, p_value = hac_mean_test(differences, config["statistics"]["hac_lag"])
                comparisons.append(
                    {"baseline_id": baseline_id, "evidence_period": role, "configuration_id": configuration_id, "observations": len(differences), "mean_daily_difference": avg, "annualized_arithmetic_difference": avg * annualization, "hac_lag": config["statistics"]["hac_lag"], "hac_t": t_stat, "p_value": p_value, "bh_q_value": math.nan, "passes_fdr": False}
                )
    for baseline_id in sorted(fixed):
        for role in roles:
            family = [(index, row["p_value"]) for index, row in enumerate(comparisons) if row["baseline_id"] == baseline_id and row["evidence_period"] == role and math.isfinite(row["p_value"])]
            for index, q_value in bh_qvalues(family).items():
                comparisons[index]["bh_q_value"] = q_value
                comparisons[index]["passes_fdr"] = q_value <= config["statistics"]["fdr_alpha"]

    development = {row["configuration_id"]: row for row in period_rows if row["evidence_period"] == "development_consumed" and row["account_type"] == "selected"}
    pareto_rows: list[dict[str, Any]] = []
    for candidate_id, row in sorted(development.items()):
        candidate = (float(row["annual_return"]), float(row["sharpe"]), float(row["max_drawdown"]))
        dominated = False
        for other_id, other_row in development.items():
            if other_id == candidate_id:
                continue
            other = (float(other_row["annual_return"]), float(other_row["sharpe"]), float(other_row["max_drawdown"]))
            if all(other[index] >= candidate[index] for index in range(3)) and any(other[index] > candidate[index] for index in range(3)):
                dominated = True
                break
        if not dominated:
            pareto_rows.append({"configuration_id": candidate_id, "annual_return": row["annual_return"], "sharpe": row["sharpe"], "max_drawdown": row["max_drawdown"]})

    ensure_empty_dir(args.output_dir)
    metric_fields = ["configuration_id", "account_type", "cumulative_return", "annual_return", "sharpe", "max_drawdown", "calmar", "average_turnover", "average_fill_rate", "average_cash_weight"]
    write_csv(args.output_dir / "account_metrics.csv", metric_fields, metric_rows)
    write_csv(args.output_dir / "period_metrics.csv", ["configuration_id", "account_type", "evidence_period", "cumulative_return", "annual_return", "sharpe", "max_drawdown", "calmar", "average_turnover", "average_fill_rate", "average_cash_weight"], period_rows)
    write_csv(args.output_dir / "annual_metrics.csv", ["configuration_id", "account_type", "deployment_id", "evidence_period", "cumulative_return", "annual_return", "sharpe", "max_drawdown", "calmar"], annual_rows)
    comparison_fields = ["baseline_id", "evidence_period", "configuration_id", "observations", "mean_daily_difference", "annualized_arithmetic_difference", "hac_lag", "hac_t", "p_value", "bh_q_value", "passes_fdr"]
    write_csv(args.output_dir / "paired_hac_vs_baselines.csv", comparison_fields, ({key: _fmt(value) if isinstance(value, float) else value for key, value in row.items()} for row in comparisons))
    write_csv(args.output_dir / "development_pareto.csv", ["configuration_id", "annual_return", "sharpe", "max_drawdown"], pareto_rows)
    fdr_count = sum(1 for row in comparisons if row["passes_fdr"])
    report = [
        "# Factor Route Ensemble Selection Report",
        "",
        f"- 固定基准：{len(fixed)}",
        f"- N 选优配置：{len(selected)}",
        f"- 配对 HAC 比较：{len(comparisons)}",
        f"- BH/FDR q≤{config['statistics']['fdr_alpha']} 通过：{fdr_count}",
        f"- 开发期 Pareto 候选：{len(pareto_rows)}",
        "",
        "本报告不自动选择冠军，不根据诊断期修改冻结配置。",
    ]
    (args.output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    artifacts = ["account_metrics.csv", "period_metrics.csv", "annual_metrics.csv", "paired_hac_vs_baselines.csv", "development_pareto.csv", "REPORT.md"]
    base = {
        "schema_version": SCHEMA_VERSION,
        "stage": "formal_account_summary_complete",
        "plan_hash": config["plan_hash"],
        "automatic_champion": False,
        "inputs": {"fixed_accounts": sha256_file(args.fixed_accounts), "selected_accounts": sha256_file(args.selected_accounts)},
        "artifacts": _artifact_hashes(args.output_dir, artifacts),
    }
    success = {**base, "status": "complete", "result_hash": stable_hash(base)}
    write_json(args.output_dir / "SUCCESS.json", success)


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "structure":
            _cmd_structure(args)
        elif args.command == "aggregate":
            _cmd_aggregate(args)
        elif args.command == "select":
            _cmd_select(args)
        else:
            _cmd_summarize(args)
        return 0
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
