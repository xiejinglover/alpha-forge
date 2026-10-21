#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from factor_evidence_core import (
    ContractError,
    adjusted_r2,
    assign_groups,
    bh_qvalues,
    file_sha256,
    hac_mean,
    hash_outputs,
    load_inputs,
    prepare_output_dir,
    read_json,
    spearman,
    write_csv,
)


DAILY_COLUMNS = [
    "plan_id", "deployment_id", "pool_id", "recipe_id", "consensus_id", "label_id", "factor_id",
    "decision_time", "pool_size", "mature_label_count", "factor_non_null_count", "unique_value_count",
    "group_count", "rank_ic", "spread", "monotonicity", "valid_for_health", "valid_for_economic", "reason",
]
HEALTH_COLUMNS = [
    "plan_id", "deployment_id", "pool_id", "recipe_id", "label_id", "factor_id", "factor_type",
    "theoretical_decisions", "required_valid_decisions", "actual_valid_decisions", "decision_coverage",
    "event_non_null_ratio", "planned_group_count", "health_pass", "model_candidate", "health_reason",
]
SUMMARY_COLUMNS = [
    "plan_id", "deployment_id", "pool_id", "recipe_id", "consensus_id", "label_id", "window_id",
    "factor_id", "factor_type", "primary_metric", "primary_n", "primary_mean", "primary_se", "primary_t",
    "primary_p", "primary_q", "direction", "spread_mean", "spread_se", "adjusted_spread_ci_lower",
    "minimum_effect", "positive_period_share", "average_adjusted_monotonicity", "statistical_pass",
    "economic_pass", "stability_pass", "window_economic_eligible", "model_candidate", "sensitivity_json",
    "disposition",
]
PERIOD_COLUMNS = [
    "plan_id", "deployment_id", "pool_id", "recipe_id", "label_id", "factor_id", "period",
    "decision_count", "adjusted_primary_mean", "adjusted_spread_mean", "adjusted_monotonicity_mean", "positive",
]
EXPOSURE_COLUMNS = [
    "plan_id", "deployment_id", "pool_id", "recipe_id", "factor_id", "exposure_id", "exposure_type",
    "valid_decisions", "mean", "median", "mean_absolute",
]
LINEAGE_COLUMNS = [
    "deployment_id", "pool_id", "consensus_id", "label_id", "factor_id", "window_count",
    "economic_windows_passed", "model_windows_passed", "direction_consistent", "economic_eligible",
    "model_candidate", "factor_set_hash",
]
FAILURE_COLUMNS = [
    "plan_id", "deployment_id", "pool_id", "recipe_id", "label_id", "factor_id", "decision_time", "reason",
]


def verify_plan(plan_dir: Path, paths: dict[str, str | None]) -> tuple[dict, list[dict]]:
    manifest = read_json(plan_dir / "input_manifest.json")
    frozen = read_json(plan_dir / "frozen_spec.json")
    for name, expected in manifest.get("plan_artifacts", {}).items():
        actual = file_sha256(plan_dir / name)
        if actual != expected:
            raise ContractError(f"frozen plan artifact changed: {name}")
    for role, info in manifest["inputs"].items():
        if role == "config":
            continue
        path = paths.get(role)
        if not path:
            raise ContractError(f"frozen plan expects input role {role}")
        if file_sha256(path) != info["sha256"]:
            raise ContractError(f"input changed since plan freeze: {role}")
    if paths.get("exposures") and "exposures" not in manifest["inputs"]:
        raise ContractError("exposures were not part of the frozen plan")
    with (plan_dir / "factor_plan.jsonl").open(encoding="utf-8") as handle:
        plan = [json.loads(line) for line in handle if line.strip()]
    if len(plan) != manifest["plan_rows"]:
        raise ContractError("factor plan row count changed")
    return frozen, plan


def date_key(value: pd.Timestamp) -> str:
    return value.isoformat()


def evaluate_plan_row(row: dict, data: dict[str, pd.DataFrame]) -> tuple[dict, dict, list[dict], list[dict], list[dict]]:
    recipe = row["recipe"]
    cutoff = pd.to_datetime(row["information_cutoff"], utc=True)
    start = pd.to_datetime(row["window_start"], utc=True)
    observations = data["observations"]
    obs = observations[
        observations["pool_id"].eq(row["pool_id"])
        & observations["label_id"].eq(row["label_id"])
        & observations["decision_time"].between(start, cutoff, inclusive="both")
    ].copy()
    factor = data["factors"][data["factors"]["factor_id"].eq(row["factor_id"])][
        ["observation_id", "factor_value"]
    ]
    obs = obs.merge(factor, on="observation_id", how="left")
    daily_rows: list[dict] = []
    failures: list[dict] = []
    period_rows: list[dict] = []
    valid_health_dates = 0
    theoretical_dates = 0
    theoretical_event_count = 0
    non_null_event_count = 0
    factor_type = row["factor_type"]
    groups = int(row["planned_group_count"])

    for decision_time, day in obs.groupby("decision_time", sort=True):
        base = {
            "plan_id": row["plan_id"], "deployment_id": row["deployment_id"], "pool_id": row["pool_id"],
            "recipe_id": row["recipe_id"], "consensus_id": row["consensus_id"], "label_id": row["label_id"],
            "factor_id": row["factor_id"], "decision_time": date_key(decision_time),
        }
        pool_size = int(day["observation_id"].nunique())
        mature = day[(day["label_exit_time"] <= cutoff) & day["label_value"].notna()].copy()
        mature_count = len(mature)
        reason = ""
        if pool_size < int(recipe["minimum_pool_size"]):
            reason = "small_pool"
        elif mature_count == 0:
            reason = "immature_or_invalid_label"
        else:
            theoretical_dates += 1
            theoretical_event_count += mature_count
            non_null_event_count += int(mature["factor_value"].notna().sum())
            non_null_ratio = mature["factor_value"].notna().mean()
            if non_null_ratio < float(recipe["factor_non_null_ratio"]):
                reason = "factor_missing"
            elif mature["factor_value"].nunique(dropna=True) < (2 if factor_type in {"binary", "nominal"} else max(groups, 5)):
                reason = "insufficient_unique_values"

        metric = {
            **base,
            "pool_size": pool_size,
            "mature_label_count": mature_count,
            "factor_non_null_count": int(mature["factor_value"].notna().sum()),
            "unique_value_count": int(mature["factor_value"].nunique(dropna=True)),
            "group_count": groups,
            "rank_ic": math.nan,
            "spread": math.nan,
            "monotonicity": math.nan,
            "valid_for_health": False,
            "valid_for_economic": False,
            "reason": reason,
        }
        if not reason:
            metric["valid_for_health"] = True
            valid_health_dates += 1
            if factor_type == "nominal":
                metric["reason"] = "nominal_no_economic_test"
            elif groups <= 0:
                metric["reason"] = "insufficient_pool_for_groups"
            else:
                valid = mature.dropna(subset=["factor_value", "label_value"]).copy()
                valid["group"] = assign_groups(valid["factor_value"], factor_type, groups)
                actual_groups = sorted(int(value) for value in valid["group"].dropna().unique())
                low_group = min(actual_groups) if actual_groups else None
                high_group = max(actual_groups) if actual_groups else None
                ordinal_raw_levels = factor_type == "ordinal" and valid["factor_value"].nunique() <= groups
                maximum_expected_group = len(actual_groups) if ordinal_raw_levels else groups
                required_groups = (
                    set(range(1, maximum_expected_group + 1))
                    if recipe["require_all_groups"]
                    else {low_group, high_group}
                )
                group_counts = valid.groupby("group").size().to_dict()
                missing_endpoint = not actual_groups or not required_groups.issubset(set(actual_groups))
                too_small = any(group_counts.get(group, 0) < int(recipe["minimum_group_size"]) for group in required_groups)
                if missing_endpoint:
                    metric["reason"] = "empty_required_group"
                elif too_small:
                    metric["reason"] = "small_group"
                else:
                    group_returns = valid.groupby("group")["label_value"].mean().sort_index()
                    metric["rank_ic"] = spearman(valid["factor_value"], valid["label_value"])
                    metric["spread"] = float(group_returns.loc[high_group] - group_returns.loc[low_group])
                    metric["monotonicity"] = spearman(
                        pd.Series(group_returns.index.to_numpy(dtype=float)),
                        pd.Series(group_returns.to_numpy(dtype=float)),
                    )
                    metric["valid_for_economic"] = np.isfinite(metric[recipe["primary_metric"]])
                    if not metric["valid_for_economic"]:
                        metric["reason"] = "invalid_primary_metric"
        daily_rows.append(metric)
        if metric["reason"] and metric["reason"] != "nominal_no_economic_test":
            failures.append({**base, "reason": metric["reason"]})

    required_valid = min(
        int(recipe["valid_decision_cap"]),
        max(int(recipe["minimum_valid_decisions"]), math.ceil(float(recipe["valid_decision_coverage"]) * theoretical_dates)),
    )
    event_non_null_ratio = non_null_event_count / theoretical_event_count if theoretical_event_count else math.nan
    decision_coverage = valid_health_dates / theoretical_dates if theoretical_dates else math.nan
    health_reason = ""
    if theoretical_dates < int(recipe["minimum_valid_decisions"]):
        health_reason = "insufficient_theoretical_decisions"
    elif event_non_null_ratio < float(recipe["factor_non_null_ratio"]):
        health_reason = "insufficient_event_coverage"
    elif valid_health_dates < required_valid:
        health_reason = "insufficient_valid_decisions"
    elif groups <= 0 and factor_type not in {"binary", "nominal"}:
        health_reason = "insufficient_pool_for_groups"
    health_pass = not health_reason
    health = {
        "plan_id": row["plan_id"], "deployment_id": row["deployment_id"], "pool_id": row["pool_id"],
        "recipe_id": row["recipe_id"], "label_id": row["label_id"], "factor_id": row["factor_id"],
        "factor_type": factor_type, "theoretical_decisions": theoretical_dates,
        "required_valid_decisions": required_valid, "actual_valid_decisions": valid_health_dates,
        "decision_coverage": decision_coverage, "event_non_null_ratio": event_non_null_ratio,
        "planned_group_count": groups, "health_pass": health_pass, "model_candidate": health_pass,
        "health_reason": health_reason,
    }

    economic_daily = [item for item in daily_rows if item["valid_for_economic"]]
    primary_values = [item[recipe["primary_metric"]] for item in economic_daily]
    spread_values = [item["spread"] for item in economic_daily]
    summary = {
        "plan_id": row["plan_id"], "deployment_id": row["deployment_id"], "pool_id": row["pool_id"],
        "recipe_id": row["recipe_id"], "consensus_id": row["consensus_id"], "label_id": row["label_id"],
        "window_id": row["window_id"], "factor_id": row["factor_id"], "factor_type": factor_type,
        "primary_metric": recipe["primary_metric"], "primary_n": len(primary_values), "primary_p": None,
        "primary_q": None, "direction": 0, "minimum_effect": float(recipe["minimum_effect"]),
        "statistical_pass": False, "economic_pass": False, "stability_pass": False,
        "window_economic_eligible": False, "model_candidate": health_pass, "disposition": health_reason or "pending",
    }
    if health_pass and factor_type != "nominal" and len(primary_values) >= required_valid:
        try:
            primary_stats = hac_mean(primary_values, int(recipe["resolved_hac_primary_lag"]), float(recipe["confidence_level"]))
            direction = 1 if primary_stats["mean"] > 0 else (-1 if primary_stats["mean"] < 0 else 0)
            adjusted_spreads = [direction * value for value in spread_values]
            spread_stats = hac_mean(adjusted_spreads, int(recipe["resolved_hac_primary_lag"]), float(recipe["confidence_level"]))
            sensitivity = {}
            for lag in recipe["hac_sensitivity_lags"]:
                try:
                    sensitivity[str(lag)] = hac_mean(primary_values, int(lag), float(recipe["confidence_level"]))
                except ContractError as exc:
                    sensitivity[str(lag)] = {"error": str(exc)}
            summary.update({
                "primary_n": primary_stats["n"], "primary_mean": primary_stats["mean"],
                "primary_se": primary_stats["se"], "primary_t": primary_stats["t"], "primary_p": primary_stats["p"],
                "direction": direction, "spread_mean": spread_stats["mean"], "spread_se": spread_stats["se"],
                "adjusted_spread_ci_lower": spread_stats["ci_lower"], "sensitivity_json": json.dumps(sensitivity, sort_keys=True),
            })
            by_period: dict[str, list[dict]] = defaultdict(list)
            for item in economic_daily:
                by_period[pd.Timestamp(item["decision_time"]).strftime("%Y")].append(item)
            positives = []
            adjusted_monotonicities = []
            for period, items in sorted(by_period.items()):
                adjusted_primary = [direction * item[recipe["primary_metric"]] for item in items]
                adjusted_spread = [direction * item["spread"] for item in items]
                monos = [direction * item["monotonicity"] for item in items if pd.notna(item["monotonicity"])]
                period_mean = float(np.mean(adjusted_primary))
                positives.append(period_mean > 0)
                adjusted_monotonicities.extend(monos)
                period_rows.append({
                    "plan_id": row["plan_id"], "deployment_id": row["deployment_id"], "pool_id": row["pool_id"],
                    "recipe_id": row["recipe_id"], "label_id": row["label_id"], "factor_id": row["factor_id"],
                    "period": period, "decision_count": len(items), "adjusted_primary_mean": period_mean,
                    "adjusted_spread_mean": float(np.mean(adjusted_spread)),
                    "adjusted_monotonicity_mean": float(np.mean(monos)) if monos else math.nan,
                    "positive": period_mean > 0,
                })
            positive_share = float(np.mean(positives)) if positives else math.nan
            average_mono = float(np.mean(adjusted_monotonicities)) if adjusted_monotonicities else math.nan
            mono_pass = factor_type == "binary" or (
                np.isfinite(average_mono) and average_mono >= float(recipe["monotonicity_threshold"])
            )
            summary["positive_period_share"] = positive_share
            summary["average_adjusted_monotonicity"] = average_mono
            summary["economic_pass"] = spread_stats["ci_lower"] >= float(recipe["minimum_effect"])
            summary["stability_pass"] = positive_share >= float(recipe["positive_period_share"]) and mono_pass
            summary["disposition"] = "pending_fdr"
        except ContractError as exc:
            summary["disposition"] = "hac_not_testable"
            failures.append({
                "plan_id": row["plan_id"], "deployment_id": row["deployment_id"], "pool_id": row["pool_id"],
                "recipe_id": row["recipe_id"], "label_id": row["label_id"], "factor_id": row["factor_id"],
                "decision_time": "", "reason": f"hac_not_testable:{exc}",
            })
    elif health_pass and factor_type == "nominal":
        summary["disposition"] = "nominal_model_candidate_only"
    elif health_pass:
        summary["disposition"] = "insufficient_economic_decisions"
    return health, summary, daily_rows, period_rows, failures


def exposure_diagnostics(plan: list[dict], data: dict[str, pd.DataFrame]) -> list[dict]:
    exposures = data["exposures"]
    if exposures.empty:
        return []
    rows: list[dict] = []
    observations = data["observations"]
    for plan_row in plan:
        if plan_row["factor_type"] == "nominal":
            continue
        cutoff = pd.to_datetime(plan_row["information_cutoff"], utc=True)
        start = pd.to_datetime(plan_row["window_start"], utc=True)
        obs = observations[
            observations["pool_id"].eq(plan_row["pool_id"])
            & observations["label_id"].eq(plan_row["label_id"])
            & observations["decision_time"].between(start, cutoff, inclusive="both")
        ][["observation_id", "decision_time"]].drop_duplicates("observation_id")
        factor = data["factors"][data["factors"]["factor_id"].eq(plan_row["factor_id"])][
            ["observation_id", "factor_value"]
        ]
        base = obs.merge(factor, on="observation_id", how="left")
        for (exposure_id, exposure_type), exposure in exposures.groupby(["exposure_id", "exposure_type"]):
            joined = base.merge(exposure[["observation_id", "exposure_value"]], on="observation_id", how="left")
            values = []
            for _, day in joined.groupby("decision_time"):
                valid = day.dropna(subset=["factor_value", "exposure_value"])
                if exposure_type == "continuous":
                    value = spearman(pd.to_numeric(valid["factor_value"]), pd.to_numeric(valid["exposure_value"]))
                else:
                    ranks = valid["factor_value"].rank(method="average", pct=True).to_numpy(dtype=float)
                    value = adjusted_r2(ranks, valid["exposure_value"])
                if np.isfinite(value):
                    values.append(float(value))
            rows.append({
                "plan_id": plan_row["plan_id"], "deployment_id": plan_row["deployment_id"],
                "pool_id": plan_row["pool_id"], "recipe_id": plan_row["recipe_id"],
                "factor_id": plan_row["factor_id"], "exposure_id": exposure_id, "exposure_type": exposure_type,
                "valid_decisions": len(values), "mean": float(np.mean(values)) if values else math.nan,
                "median": float(np.median(values)) if values else math.nan,
                "mean_absolute": float(np.mean(np.abs(values))) if values else math.nan,
            })
    return rows


def apply_fdr_and_consensus(summaries: list[dict], plan: list[dict]) -> list[dict]:
    by_family: dict[tuple, list[int]] = defaultdict(list)
    for index, row in enumerate(summaries):
        family = (row["deployment_id"], row["pool_id"], row["recipe_id"], row["label_id"], row["primary_metric"])
        by_family[family].append(index)
    recipe_by_id = {row["recipe_id"]: row["recipe"] for row in plan}
    for family, indices in by_family.items():
        q_values = bh_qvalues([summaries[index].get("primary_p") for index in indices])
        for index, q_value in zip(indices, q_values):
            row = summaries[index]
            row["primary_q"] = q_value
            alpha = float(recipe_by_id[row["recipe_id"]]["fdr_alpha"])
            row["statistical_pass"] = q_value is not None and q_value <= alpha
            row["window_economic_eligible"] = bool(
                row["model_candidate"] and row["statistical_pass"] and row["economic_pass"] and row["stability_pass"]
            )
            if row["window_economic_eligible"]:
                row["disposition"] = "economic_eligible_window"
            elif not row["model_candidate"]:
                row["disposition"] = row["disposition"] if row["disposition"] != "pending" else "health_failed"
            elif not row["statistical_pass"] and row.get("primary_p") is not None:
                row["disposition"] = "fdr_failed"
            elif row["statistical_pass"] and not row["economic_pass"]:
                row["disposition"] = "economic_effect_failed"
            elif row["statistical_pass"] and row["economic_pass"] and not row["stability_pass"]:
                row["disposition"] = "stability_failed"

    lineages: list[dict] = []
    by_consensus: dict[tuple, list[dict]] = defaultdict(list)
    for row in summaries:
        key = (row["deployment_id"], row["pool_id"], row["consensus_id"], row["label_id"], row["factor_id"])
        by_consensus[key].append(row)
    eligible_sets: dict[tuple, list[str]] = defaultdict(list)
    for key, rows in sorted(by_consensus.items()):
        recipes = [recipe_by_id[row["recipe_id"]] for row in rows]
        minimum = max(int(recipe["minimum_consensus_windows"]) for recipe in recipes)
        economic_count = sum(bool(row["window_economic_eligible"]) for row in rows)
        model_count = sum(bool(row["model_candidate"]) for row in rows)
        directions = {int(row["direction"]) for row in rows if row["window_economic_eligible"] and int(row["direction"]) != 0}
        require_consistent = any(bool(recipe["require_consistent_direction"]) for recipe in recipes)
        direction_consistent = len(directions) <= 1
        economic_eligible = economic_count >= minimum and (direction_consistent or not require_consistent)
        model_candidate = model_count >= minimum
        deployment_id, pool_id, consensus_id, label_id, factor_id = key
        if economic_eligible:
            eligible_sets[(deployment_id, pool_id, consensus_id, label_id)].append(factor_id)
        lineages.append({
            "deployment_id": deployment_id, "pool_id": pool_id, "consensus_id": consensus_id,
            "label_id": label_id, "factor_id": factor_id, "window_count": len(rows),
            "economic_windows_passed": economic_count, "model_windows_passed": model_count,
            "direction_consistent": direction_consistent, "economic_eligible": economic_eligible,
            "model_candidate": model_candidate, "factor_set_hash": "",
        })
    for row in lineages:
        key = (row["deployment_id"], row["pool_id"], row["consensus_id"], row["label_id"])
        names = sorted(eligible_sets.get(key, []))
        row["factor_set_hash"] = __import__("hashlib").sha256("\n".join(names).encode("utf-8")).hexdigest()
    return lineages


def build_report(summaries: list[dict], lineages: list[dict], failures: list[dict]) -> str:
    return "\n".join([
        "# 滚动因子证据报告",
        "",
        f"- 计划因子行：{len(summaries)}",
        f"- 窗口内经济合格：{sum(bool(row['window_economic_eligible']) for row in summaries)}",
        f"- 共识经济合格：{sum(bool(row['economic_eligible']) for row in lineages)}",
        f"- 共识模型候选：{sum(bool(row['model_candidate']) for row in lineages)}",
        f"- 失败或不可检验记录：{len(failures)}",
        "",
        "本报告只证明单因子边际证据，不证明去冗余后的条件增量或正式账户收益。",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a frozen rolling factor evidence plan")
    parser.add_argument("--plan-dir", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--factors", required=True)
    parser.add_argument("--factor-metadata", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--exposures")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        paths = {
            "observations": args.observations, "factors": args.factors,
            "factor_metadata": args.factor_metadata, "labels": args.labels, "exposures": args.exposures,
        }
        plan_dir = Path(args.plan_dir)
        frozen, plan = verify_plan(plan_dir, paths)
        data = load_inputs(args.observations, args.factors, args.factor_metadata, args.labels, args.exposures)
        output = prepare_output_dir(args.output_dir)

        health_rows: list[dict] = []
        summaries: list[dict] = []
        daily_rows: list[dict] = []
        period_rows: list[dict] = []
        failures: list[dict] = []
        for row in plan:
            health, summary, daily, periods, row_failures = evaluate_plan_row(row, data)
            health_rows.append(health)
            summaries.append(summary)
            daily_rows.extend(daily)
            period_rows.extend(periods)
            failures.extend(row_failures)
        lineages = apply_fdr_and_consensus(summaries, plan)
        exposure_rows = exposure_diagnostics(plan, data)

        write_csv(output / "factor_health.csv", health_rows, HEALTH_COLUMNS)
        write_csv(output / "daily_factor_metrics.csv", daily_rows, DAILY_COLUMNS)
        write_csv(output / "factor_summary.csv", summaries, SUMMARY_COLUMNS)
        write_csv(output / "period_stability.csv", period_rows, PERIOD_COLUMNS)
        write_csv(output / "exposure_diagnostics.csv", exposure_rows, EXPOSURE_COLUMNS)
        write_csv(output / "period_factor_lineage.csv", lineages, LINEAGE_COLUMNS)
        write_csv(output / "failure_records.csv", failures, FAILURE_COLUMNS)
        (output / "REPORT.md").write_text(build_report(summaries, lineages, failures), encoding="utf-8")
        output_names = [
            "factor_health.csv", "daily_factor_metrics.csv", "factor_summary.csv", "period_stability.csv",
            "exposure_diagnostics.csv", "period_factor_lineage.csv", "failure_records.csv", "REPORT.md",
        ]
        success = {
            "study_id": frozen["study_id"], "status": "complete", "plan_rows": len(plan),
            "outputs": hash_outputs(output, output_names), "automatic_winner_selected": False,
            "proves_model_increment": False, "proves_portfolio_performance": False,
        }
        (output / "SUCCESS.json").write_text(json.dumps(success, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"completed {len(plan)} factor evidence rows in {output}")
        return 0
    except (ContractError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
