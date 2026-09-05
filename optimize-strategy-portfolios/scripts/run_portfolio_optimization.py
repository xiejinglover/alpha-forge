#!/usr/bin/env python3
"""Run frozen rolling equal-sleeve strategy portfolio optimization."""

from __future__ import annotations

import argparse
import math
import platform
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from portfolio_optimization_core import (
    ContractError,
    RISK_KEYS,
    SCHEMA_VERSION,
    compound_return,
    load_study,
    load_selection_spec,
    object_hash,
    ols_with_hac,
    pairwise_values,
    parse_date,
    performance_metrics,
    portfolio_series,
    prepare_output_dir,
    read_controls,
    read_diagnostics,
    read_prior_ledger,
    read_return_panel,
    risk_metrics,
    select_fixed_schemes,
    sha256_file,
    write_csv,
    write_json,
)


CANDIDATE_FIELDS = [
    "deployment_id", "candidate_id", "eligible", "exclusion_reasons", "robust_quality_score",
    "quality_rank", "in_quality_pool", "estimation_compound_return", "estimation_volatility",
    "ordinary_beta", "controlled_beta", "downside_beta", "tail_10_beta", "tail_5_beta",
    "common_loss_rate", "residual_alpha", "residual_alpha_hac_se", "observations",
]
MEMBER_FIELDS = ["deployment_id", "scheme", "n", "candidate_id", "weight", "cluster_id"]
RETURN_FIELDS = ["deployment_id", "scheme", "n", "period", "date", "net_return"]
METRIC_FIELDS = [
    "deployment_id", "scheme", "n", "period", "benchmark_id", "observations",
    "annual_return", "sharpe", "max_drawdown", "ordinary_beta", "controlled_beta",
    "downside_beta", "tail_10_beta", "tail_5_beta", "common_loss_rate",
    "residual_alpha", "residual_alpha_hac_se", "controls_supplied",
]
PAIR_FIELDS = [
    "deployment_id", "scheme", "n", "candidate_id_a", "candidate_id_b",
    "raw_return_correlation", "residual_correlation", "signal_jaccard",
]
INFEASIBLE_FIELDS = ["deployment_id", "scheme", "n", "stage", "reason", "detail"]
LEDGER_FIELDS = [
    "deployment_id", "dataset_id", "access_order", "event_time", "purpose",
    "used_for_selection", "status", "selection_spec_hash", "evidence_role",
    "previous_event_hash", "event_hash",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True, type=Path)
    parser.add_argument("--candidate-returns", required=True, type=Path)
    parser.add_argument("--benchmark-returns", required=True, type=Path)
    parser.add_argument("--diagnostics", required=True, type=Path)
    parser.add_argument("--controls", type=Path)
    parser.add_argument("--prior-oos-ledger", type=Path)
    parser.add_argument("--selection-spec", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def _days(series: Mapping[date, float], start: date, end: date) -> list[date]:
    return sorted(day for day in series if start <= day <= end)


def _control_matrix(days: Sequence[date], controls: Mapping[date, Sequence[float]], supplied: bool) -> list[list[float]]:
    if not supplied:
        return [[] for _ in days]
    missing = [day for day in days if day not in controls]
    if missing:
        raise ContractError(f"control returns missing {missing[0].isoformat()}")
    return [list(controls[day]) for day in days]


def _fmt(value: Any) -> Any:
    if isinstance(value, float):
        return "" if not math.isfinite(value) else format(value, ".17g")
    return value


def _metric_row(
    deployment_id: str, scheme: str, n: int, period: str, benchmark_id: str,
    portfolio_returns: Sequence[float], benchmark_returns: Sequence[float],
    controls: Sequence[Sequence[float]], annualization: int, hac_lags: int,
    controls_supplied: bool,
) -> dict[str, Any]:
    performance = performance_metrics(portfolio_returns, annualization)
    risk, _ = risk_metrics(portfolio_returns, benchmark_returns, controls, annualization, hac_lags)
    return {
        "deployment_id": deployment_id, "scheme": scheme, "n": n, "period": period,
        "benchmark_id": benchmark_id, **performance, **risk,
        "controls_supplied": str(controls_supplied).lower(),
    }


def run(args: argparse.Namespace) -> None:
    study = load_study(args.study)
    candidate_panel = read_return_panel(args.candidate_returns, "candidate_id")
    benchmark_panel = read_return_panel(args.benchmark_returns, "benchmark_id")
    diagnostics = read_diagnostics(args.diagnostics)
    factor_ids, controls = read_controls(args.controls)
    prior_ledger = read_prior_ledger(args.prior_oos_ledger)
    selection_spec = load_selection_spec(args.selection_spec)
    actual_selection_spec_hash = sha256_file(args.selection_spec) if args.selection_spec else ""
    provenance_sources: dict[str, str] = {}
    for record in diagnostics.values():
        source_path = Path(record["source_artifact_path"])
        expected_hash = str(record["source_artifact_sha256"])
        if not source_path.is_file():
            raise ContractError(f"provenance source does not exist: {source_path}")
        actual_hash = sha256_file(source_path)
        if actual_hash != expected_hash:
            raise ContractError(f"provenance hash mismatch: {source_path}")
        provenance_sources[str(source_path)] = actual_hash
    controls_supplied = args.controls is not None
    prepare_output_dir(args.output_dir)

    annualization = int(study["annualization"])
    quality_top_k = int(study["quality_top_k"])
    hac_lags = int(study["hac_lags"])
    minimum_periods = int(study.get("minimum_positive_residual_periods", 0))
    n_values = [int(value) for value in study["n_values"]]
    primary_benchmark_ids = {str(item["benchmark_id"]) for item in study["deployments"]}
    if len(primary_benchmark_ids) != 1:
        raise ContractError("one run must represent one strategy family with one primary benchmark")
    primary_benchmark_id = next(iter(primary_benchmark_ids))

    candidate_rows: list[dict[str, Any]] = []
    member_rows: list[dict[str, Any]] = []
    return_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    infeasible_rows: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    for row in prior_ledger:
        ledger_rows.append({
            "deployment_id": "", "dataset_id": row["dataset_id"],
            "access_order": row["access_order"], "event_time": row["event_time"].isoformat(),
            "purpose": row["purpose"], "used_for_selection": str(row["used_for_selection"]).lower(),
            "status": row["status"], "selection_spec_hash": row["selection_spec_hash"],
            "evidence_role": "prior_record",
            "previous_event_hash": row["previous_event_hash"], "event_hash": row["event_hash"],
        })

    for deployment in study["deployments"]:
        deployment_id = str(deployment["deployment_id"])
        benchmark_id = str(deployment["benchmark_id"])
        if benchmark_id not in benchmark_panel:
            raise ContractError(f"deployment {deployment_id} unknown benchmark {benchmark_id}")
        est_start = parse_date(deployment["estimation"]["start"])
        est_end = parse_date(deployment["estimation"]["end"])
        eval_start = parse_date(deployment["evaluation"]["start"])
        eval_end = parse_date(deployment["evaluation"]["end"])
        est_days = _days(benchmark_panel[benchmark_id], est_start, est_end)
        eval_days = _days(benchmark_panel[benchmark_id], eval_start, eval_end)
        if len(est_days) < 3 or len(eval_days) < 3:
            raise ContractError(f"deployment {deployment_id} benchmark has fewer than three rows in a window")
        est_benchmark = [benchmark_panel[benchmark_id][day] for day in est_days]
        est_controls = _control_matrix(est_days, controls, controls_supplied)
        _control_matrix(eval_days, controls, controls_supplied)

        dataset_id = str(deployment["evaluation_dataset_id"])
        dataset_history = [row for row in prior_ledger if row["dataset_id"] == dataset_id]
        role = str(deployment["evidence_role"])
        selection_spec_hash = str(deployment.get("selection_spec_hash", ""))
        accessed_at = datetime.fromisoformat(str(deployment["evaluation_accessed_at"]))
        if role in {"independent_holdout", "forward_monitoring"}:
            if selection_spec is None:
                raise ContractError(f"deployment {deployment_id} requires a frozen selection-spec artifact")
            if actual_selection_spec_hash != selection_spec_hash:
                raise ContractError(f"deployment {deployment_id} selection-spec artifact hash mismatch")
            if selection_spec["family_id"] != study["family_id"]:
                raise ContractError(f"deployment {deployment_id} selection-spec family mismatch")
            if selection_spec["frozen_at"] != deployment["selection_frozen_at"]:
                raise ContractError(f"deployment {deployment_id} selection-spec freeze time mismatch")
            if selection_spec.get("decision_rules") != study.get("decision_rules"):
                raise ContractError(f"deployment {deployment_id} decision rules were not frozen in selection spec")
            if not dataset_history:
                raise ContractError(
                    f"deployment {deployment_id} requires prior locked ledger evidence for {dataset_id}"
                )
            latest = max(dataset_history, key=lambda row: int(row["access_order"]))
            if any(row["used_for_selection"] for row in dataset_history):
                raise ContractError(f"dataset {dataset_id} was already used for selection")
            if latest["status"] not in {"prepared_not_accessed", "locked_unconsumed"}:
                raise ContractError(f"dataset {dataset_id} is not in an unconsumed locked state")
            if latest["selection_spec_hash"] != selection_spec_hash:
                raise ContractError(f"dataset {dataset_id} selection-spec hash mismatch")
            frozen_dataset = selection_spec["datasets"].get(dataset_id)
            if not isinstance(frozen_dataset, dict):
                raise ContractError(f"selection spec does not freeze dataset {dataset_id}")
            expected_dataset = {
                "estimation_start": est_start.isoformat(), "estimation_end": est_end.isoformat(),
                "evaluation_start": eval_start.isoformat(), "evaluation_end": eval_end.isoformat(),
                "benchmark_id": benchmark_id,
            }
            if any(frozen_dataset.get(key) != value for key, value in expected_dataset.items()):
                raise ContractError(f"selection spec dataset contract mismatch for {dataset_id}")
            if latest["event_time"] > accessed_at:
                raise ContractError(f"dataset {dataset_id} ledger event occurs after declared access")
        access_order = max((int(row["access_order"]) for row in dataset_history), default=0) + 1
        previous_event_hash = (
            max(dataset_history, key=lambda row: int(row["access_order"]))["event_hash"]
            if dataset_history else ""
        )
        event_payload = {
            "dataset_id": dataset_id, "access_order": access_order,
            "event_time": accessed_at.isoformat(), "purpose": "frozen_portfolio_evaluation",
            "used_for_selection": bool(deployment["consumed_for_selection"]),
            "status": (
                "consumed_research" if deployment["consumed_for_selection"]
                else "accessed_for_forward_monitoring" if role == "forward_monitoring"
                else "accessed_for_confirmation"
            ),
            "selection_spec_hash": selection_spec_hash,
            "previous_event_hash": previous_event_hash,
        }
        ledger_rows.append({
            "deployment_id": deployment_id, "dataset_id": dataset_id,
            "access_order": access_order, "event_time": accessed_at.isoformat(),
            "purpose": "frozen_portfolio_evaluation",
            "used_for_selection": str(deployment["consumed_for_selection"]).lower(),
            "status": event_payload["status"],
            "selection_spec_hash": selection_spec_hash,
            "evidence_role": role,
            "previous_event_hash": previous_event_hash,
            "event_hash": object_hash(event_payload),
        })

        deployment_candidates = sorted(
            candidate for dep, candidate in diagnostics if dep == deployment_id
        )
        if not deployment_candidates:
            raise ContractError(f"deployment {deployment_id} has no diagnostics")
        missing_diagnostics = sorted(set(candidate_panel) - set(deployment_candidates))
        unknown_diagnostics = sorted(set(deployment_candidates) - set(candidate_panel))
        if missing_diagnostics or unknown_diagnostics:
            raise ContractError(
                f"deployment {deployment_id} diagnostic denominator mismatch: "
                f"missing={missing_diagnostics[:5]}, unknown={unknown_diagnostics[:5]}"
            )
        local_rows: list[dict[str, Any]] = []
        residuals: dict[str, list[float]] = {}
        raw_estimation: dict[str, list[float]] = {}
        for candidate in deployment_candidates:
            diag = diagnostics[(deployment_id, candidate)]
            reasons: list[str] = []
            if diag["family_id"] != str(study["family_id"]):
                reasons.append("family_id_mismatch")
            if diag["estimation_start"] != est_start or diag["estimation_end"] != est_end:
                reasons.append("diagnostic_window_mismatch")
            if diag["as_of"] != est_end:
                reasons.append("diagnostic_as_of_mismatch")
            if not diag["eligible"]:
                reasons.append("source_ineligible")
            if not diag["trading_activity_ok"]:
                reasons.append("trading_activity_failed")
            if not diag["coverage_complete"]:
                reasons.append("source_coverage_failed")
            if diag["positive_residual_periods"] < minimum_periods:
                reasons.append("residual_stability_failed")
            series = candidate_panel.get(candidate, {})
            if any(day not in series for day in est_days):
                reasons.append("estimation_return_coverage_incomplete")
            values = [series[day] for day in est_days if day in series]
            compound = compound_return(values) if len(values) == len(est_days) else math.nan
            volatility = float(np.std(values, ddof=1)) if len(values) > 1 else math.nan
            if math.isfinite(compound) and compound <= 0:
                reasons.append("nonpositive_estimation_return")
            if math.isfinite(volatility) and volatility <= 0:
                reasons.append("zero_estimation_volatility")
            metrics: dict[str, float] = {
                "ordinary_beta": math.nan, "controlled_beta": math.nan,
                "downside_beta": math.nan, "tail_10_beta": math.nan, "tail_5_beta": math.nan,
                "common_loss_rate": math.nan, "residual_alpha": math.nan,
                "residual_alpha_hac_se": math.nan, "observations": float(len(values)),
            }
            if len(values) == len(est_days):
                try:
                    metrics, candidate_residuals = risk_metrics(
                        values, est_benchmark, est_controls, annualization, hac_lags
                    )
                    residuals[candidate] = candidate_residuals
                    raw_estimation[candidate] = values
                    if metrics["residual_alpha"] <= 0:
                        reasons.append("nonpositive_residual_alpha")
                    if any(not math.isfinite(metrics[key]) for key in RISK_KEYS):
                        reasons.append("nonfinite_selection_beta")
                except ContractError as exc:
                    reasons.append(f"risk_regression_failed:{exc}")
            row = {
                "deployment_id": deployment_id,
                "candidate_id": candidate,
                "eligible": not reasons,
                "exclusion_reasons": "|".join(reasons),
                "robust_quality_score": diag["robust_quality_score"],
                "quality_rank": "",
                "in_quality_pool": False,
                "estimation_compound_return": compound,
                "estimation_volatility": volatility,
                **metrics,
            }
            local_rows.append(row)

        eligible = sorted(
            (row for row in local_rows if row["eligible"]),
            key=lambda row: (-float(row["robust_quality_score"]), str(row["candidate_id"])),
        )
        quality_pool = eligible[:quality_top_k]
        ranks = {str(row["candidate_id"]): index for index, row in enumerate(eligible, 1)}
        quality_ids = {str(row["candidate_id"]) for row in quality_pool}
        for row in local_rows:
            candidate = str(row["candidate_id"])
            row["quality_rank"] = ranks.get(candidate, "")
            row["in_quality_pool"] = candidate in quality_ids
            candidate_rows.append({key: _fmt(row.get(key)) for key in CANDIDATE_FIELDS})
        selections: dict[tuple[str, int], list[str]] = {}
        for n in n_values:
            fixed = select_fixed_schemes(quality_pool, n)
            if not fixed:
                missing_schemes = ["LOW_BETA_EQ"]
                for scheme in missing_schemes:
                    infeasible_rows.append({
                        "deployment_id": deployment_id, "scheme": scheme, "n": n,
                        "stage": "selection", "reason": "insufficient_quality_candidates",
                        "detail": f"quality_pool={len(quality_pool)}",
                    })
                continue
            for scheme, members in fixed.items():
                selections[(scheme, n)] = members

        comparison_ids = [benchmark_id] + [
            str(item) for item in deployment.get("comparison_benchmark_ids", []) if str(item) != benchmark_id
        ]
        for comparison_id in comparison_ids:
            if comparison_id not in benchmark_panel:
                raise ContractError(f"deployment {deployment_id} unknown comparison benchmark {comparison_id}")

        for (scheme, n), members in sorted(selections.items()):
            for candidate in members:
                member_rows.append({
                    "deployment_id": deployment_id, "scheme": scheme, "n": n,
                    "candidate_id": candidate, "weight": _fmt(1.0 / n),
                    "cluster_id": "",
                })
            for first_index, first in enumerate(sorted(members)):
                for second in sorted(members)[first_index + 1:]:
                    raw_corr = pairwise_values([first, second], raw_estimation)
                    residual_corr = pairwise_values([first, second], residuals)
                    pair_rows.append({
                        "deployment_id": deployment_id, "scheme": scheme, "n": n,
                        "candidate_id_a": first, "candidate_id_b": second,
                        "raw_return_correlation": _fmt(raw_corr[0] if raw_corr else math.nan),
                        "residual_correlation": _fmt(residual_corr[0] if residual_corr else math.nan),
                        "signal_jaccard": "",
                    })
            try:
                period_series = {
                    "estimation": (est_days, portfolio_series(members, candidate_panel, est_days)),
                    "evaluation": (eval_days, portfolio_series(members, candidate_panel, eval_days)),
                }
            except ContractError as exc:
                infeasible_rows.append({
                    "deployment_id": deployment_id, "scheme": scheme, "n": n,
                    "stage": "evaluation", "reason": "return_coverage_incomplete", "detail": str(exc),
                })
                continue
            for period, (days, values) in period_series.items():
                for day, value in zip(days, values):
                    return_rows.append({
                        "deployment_id": deployment_id, "scheme": scheme, "n": n,
                        "period": period, "date": day.isoformat(), "net_return": _fmt(value),
                    })
                for comparison_id in comparison_ids:
                    comparison_days = _days(
                        benchmark_panel[comparison_id],
                        est_start if period == "estimation" else eval_start,
                        est_end if period == "estimation" else eval_end,
                    )
                    if comparison_days != days:
                        infeasible_rows.append({
                            "deployment_id": deployment_id, "scheme": scheme, "n": n,
                            "stage": f"{period}_cross_benchmark", "reason": "benchmark_calendar_mismatch",
                            "detail": comparison_id,
                        })
                        continue
                    comparison_returns = [benchmark_panel[comparison_id][day] for day in days]
                    control_matrix = _control_matrix(days, controls, controls_supplied)
                    try:
                        metric = _metric_row(
                            deployment_id, scheme, n, period, comparison_id, values,
                            comparison_returns, control_matrix, annualization, hac_lags,
                            controls_supplied,
                        )
                    except ContractError as exc:
                        infeasible_rows.append({
                            "deployment_id": deployment_id, "scheme": scheme, "n": n,
                            "stage": f"{period}_metrics", "reason": "risk_regression_failed",
                            "detail": f"{comparison_id}:{exc}",
                        })
                        continue
                    metric_rows.append({key: _fmt(metric.get(key)) for key in METRIC_FIELDS})

    deployment_ids = {str(item["deployment_id"]) for item in study["deployments"]}
    evaluation_rows = [row for row in return_rows if row["period"] == "evaluation"]
    configurations = sorted({(row["scheme"], int(row["n"])) for row in member_rows})
    for scheme, n in configurations:
        selected = [row for row in evaluation_rows if row["scheme"] == scheme and int(row["n"]) == n]
        covered = {row["deployment_id"] for row in selected}
        if covered != deployment_ids:
            infeasible_rows.append({
                "deployment_id": "ALL_ROLLING", "scheme": scheme, "n": n,
                "stage": "aggregation", "reason": "incomplete_deployment_coverage",
                "detail": f"covered={sorted(covered)}",
            })
            continue
        by_date: dict[date, float] = {}
        duplicate = None
        for row in selected:
            day = parse_date(row["date"])
            if day in by_date:
                duplicate = day
                break
            by_date[day] = float(row["net_return"])
        if duplicate is not None:
            infeasible_rows.append({
                "deployment_id": "ALL_ROLLING", "scheme": scheme, "n": n,
                "stage": "aggregation", "reason": "overlapping_evaluation_dates",
                "detail": duplicate.isoformat(),
            })
            continue
        days = sorted(by_date)
        values = [by_date[day] for day in days]
        benchmark_values = [benchmark_panel[primary_benchmark_id][day] for day in days]
        control_matrix = _control_matrix(days, controls, controls_supplied)
        for day, value in zip(days, values):
            return_rows.append({
                "deployment_id": "ALL_ROLLING", "scheme": scheme, "n": n,
                "period": "rolling_evaluation", "date": day.isoformat(), "net_return": _fmt(value),
            })
        metric = _metric_row(
            "ALL_ROLLING", scheme, n, "rolling_evaluation", primary_benchmark_id,
            values, benchmark_values, control_matrix, annualization, hac_lags, controls_supplied,
        )
        metric_rows.append({key: _fmt(metric.get(key)) for key in METRIC_FIELDS})

    artifacts = {
        "candidate_risk_metrics.csv": (CANDIDATE_FIELDS, candidate_rows),
        "portfolio_members.csv": (MEMBER_FIELDS, member_rows),
        "portfolio_returns.csv": (RETURN_FIELDS, return_rows),
        "portfolio_metrics.csv": (METRIC_FIELDS, metric_rows),
        "pairwise_diagnostics.csv": (PAIR_FIELDS, pair_rows),
        "infeasible.csv": (INFEASIBLE_FIELDS, infeasible_rows),
        "oos_access_ledger.csv": (LEDGER_FIELDS, ledger_rows),
    }
    for filename, (fields, rows) in artifacts.items():
        write_csv(args.output_dir / filename, fields, rows)

    input_paths = {
        "study": args.study, "candidate_returns": args.candidate_returns,
        "benchmark_returns": args.benchmark_returns, "diagnostics": args.diagnostics,
        "controls": args.controls,
        "prior_oos_ledger": args.prior_oos_ledger,
        "selection_spec": args.selection_spec,
    }
    data_artifact_hashes = {filename: sha256_file(args.output_dir / filename) for filename in artifacts}
    research_record = {
        "schema_version": SCHEMA_VERSION,
        "stage": "portfolio_optimization_complete",
        "study": study,
        "controls": {"supplied": controls_supplied, "factor_ids": factor_ids},
        "environment": {"python": platform.python_version(), "numpy": np.__version__},
        "inputs": {
            name: {"path": str(path.resolve()), "sha256": sha256_file(path)}
            for name, path in input_paths.items() if path is not None
        },
        "provenance_sources": [
            {"path": path, "sha256": value} for path, value in sorted(provenance_sources.items())
        ],
        "artifacts": data_artifact_hashes,
        "summary": {
            "candidate_rows": len(candidate_rows), "member_rows": len(member_rows),
            "metric_rows": len(metric_rows), "infeasible_rows": len(infeasible_rows),
        },
    }
    research_hash = object_hash(research_record)
    report_context = {**research_record, "research_hash": research_hash}
    from build_portfolio_report import render_report
    report_path = args.output_dir / "report.md"
    report_path.write_text(render_report(report_context, args.output_dir), encoding="utf-8")
    final_without_hash = {
        **report_context,
        "artifacts": {**data_artifact_hashes, "report.md": sha256_file(report_path)},
    }
    manifest = {**final_without_hash, "run_hash": object_hash(final_without_hash)}
    write_json(args.output_dir / "frozen_spec.json", manifest)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        run(args)
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
