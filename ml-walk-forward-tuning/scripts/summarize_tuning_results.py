#!/usr/bin/env python3
"""Validate tuning results and build complete human-decision summaries."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from tuning_core import (
    ContractError,
    canonical_json,
    ensure_empty_output_dir,
    file_sha256,
    format_number,
    load_json,
    load_results,
    mean_or_none,
    pareto_ids,
    read_jsonl,
    stable_hash,
    stdev_or_none,
    validate_config,
    write_csv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def verify_plan(plan_dir: Path) -> tuple[dict, dict, list[dict], list[dict]]:
    manifest_path = plan_dir / "study_manifest.json"
    config_path = plan_dir / "study_config.normalized.json"
    candidate_path = plan_dir / "candidate_configs.jsonl"
    trial_path = plan_dir / "trial_plan.jsonl"
    for path in (manifest_path, config_path, candidate_path, trial_path):
        if not path.is_file():
            raise ContractError(f"missing plan artifact: {path}")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ContractError("unsupported study manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ContractError("study manifest is missing artifact hashes")
    for path in (config_path, candidate_path, trial_path):
        expected = artifacts.get(path.name)
        if not isinstance(expected, str) or file_sha256(path) != expected:
            raise ContractError(f"frozen plan artifact changed: {path.name}")
    config = validate_config(load_json(config_path))
    if stable_hash(config) != manifest.get("config_sha256"):
        raise ContractError("normalized config hash does not match the manifest")
    candidates = read_jsonl(candidate_path)
    trials = read_jsonl(trial_path)
    if len(candidates) != manifest.get("counts", {}).get("candidate_count"):
        raise ContractError("candidate count does not match the manifest")
    if len(trials) != manifest.get("counts", {}).get("trial_count"):
        raise ContractError("trial count does not match the manifest")
    candidate_ids = [row.get("candidate_id") for row in candidates]
    trial_ids = [row.get("trial_id") for row in trials]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ContractError("candidate plan contains duplicate candidate_id")
    if len(trial_ids) != len(set(trial_ids)):
        raise ContractError("trial plan contains duplicate trial_id")
    known_candidates = set(candidate_ids)
    validation_ids = {item["id"] for item in config["development"]["validation_periods"]}
    seeds = set(config["seeds"])
    development_end = config["development"]["validation_periods"][-1]["end"]
    for row in trials:
        if row.get("candidate_id") not in known_candidates:
            raise ContractError("trial plan references an unknown candidate")
        if row.get("validation_id") not in validation_ids:
            raise ContractError("trial plan references an unknown validation period")
        if row.get("seed") not in seeds:
            raise ContractError("trial plan references an unknown seed")
        validation = row.get("validation") or {}
        if validation.get("end", "") > development_end:
            raise ContractError("trial plan extends beyond the development end")
    return manifest, config, candidates, trials


def render_report(
    manifest: dict,
    metrics: list[dict[str, str]],
    completeness_rows: list[dict],
    ranking_rows: list[dict],
    pareto_rows: list[dict],
) -> str:
    complete_count = sum(1 for row in completeness_rows if row["complete"] == "true")
    incomplete_count = len(completeness_rows) - complete_count
    failed_trials = sum(int(row["failed_trials"]) for row in completeness_rows)
    missing_trials = sum(int(row["missing_trials"]) for row in completeness_rows)
    lines = [
        "# 机器学习滚动调参报告",
        "",
        "> 本报告只汇总开发期滚动验证，不构成最终样本外结论，不自动选择或冻结配置。",
        "",
        "## 研究分母",
        "",
        f"- 候选配置：{manifest['counts']['candidate_count']}",
        f"- 计划 trial：{manifest['counts']['trial_count']}",
        f"- 完整候选：{complete_count}",
        f"- 不完整候选：{incomplete_count}",
        f"- 失败 trial：{failed_trials}",
        f"- 缺失 trial：{missing_trials}",
        "",
        "## 指标方向",
        "",
        "| 指标 | 方向 |",
        "|---|---|",
    ]
    lines.extend(f"| {item['name']} | {item['direction']} |" for item in metrics)
    lines.extend(["", "## 各指标前列候选", ""])
    if ranking_rows:
        lines.extend(["| 指标 | 排名 | 候选 | 跨验证期均值 |", "|---|---:|---|---:|"])
        for row in ranking_rows:
            if int(row["rank"]) <= 10:
                lines.append(f"| {row['metric']} | {row['rank']} | {row['candidate_id']} | {row['mean']} |")
    else:
        lines.append("没有结果完整、可进入排名的候选。")
    lines.extend(["", "## Pareto 候选", ""])
    if pareto_rows:
        headers = ["candidate_id"] + [item["name"] for item in metrics]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "---|" * len(headers))
        for row in pareto_rows:
            lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    else:
        lines.append("没有结果完整、可进入 Pareto 比较的候选。")
    lines.extend(
        [
            "",
            "## 人工决策说明",
            "",
            "请同时查看逐验证期表现、跨期最差值、标准差、完整性和候选完整配置。",
            "本报告不提供综合分或唯一推荐；任何后续配置冻结应作为独立步骤执行。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        manifest, config, candidates, trials = verify_plan(args.plan_dir)
        metrics = config["metrics"]
        metric_names = [item["name"] for item in metrics]
        metric_directions = {item["name"]: item["direction"] for item in metrics}
        result_state = load_results(args.results, trials, metrics)
        if result_state["incomplete_completed"]:
            examples = ", ".join(
                f"{trial_id} missing {','.join(names)}"
                for trial_id, names in list(sorted(result_state["incomplete_completed"].items()))[:5]
            )
            raise ContractError(f"completed trials must report every metric: {examples}")
        completed = result_state["completed"]
        failed = result_state["failed"]
        ensure_empty_output_dir(args.output_dir)

        all_rows: list[dict] = []
        for trial in trials:
            trial_id = trial["trial_id"]
            if trial_id in completed:
                state = completed[trial_id]
                for metric in metric_names:
                    all_rows.append(
                        {
                            "trial_id": trial_id,
                            "candidate_id": trial["candidate_id"],
                            "validation_id": trial["validation_id"],
                            "seed": trial["seed"],
                            "metric": metric,
                            "value": format_number(state["metrics"][metric]),
                            "status": "completed",
                            "artifact_path": state["artifact_path"],
                            "error": "",
                            "actual_iterations": state["actual_iterations"] or "",
                        }
                    )
            elif trial_id in failed:
                state = failed[trial_id]
                for metric in metric_names:
                    all_rows.append(
                        {
                            "trial_id": trial_id,
                            "candidate_id": trial["candidate_id"],
                            "validation_id": trial["validation_id"],
                            "seed": trial["seed"],
                            "metric": metric,
                            "value": "",
                            "status": "failed",
                            "artifact_path": state["artifact_path"],
                            "error": state["error"],
                            "actual_iterations": state["actual_iterations"] or "",
                        }
                    )
            else:
                for metric in metric_names:
                    all_rows.append(
                        {
                            "trial_id": trial_id,
                            "candidate_id": trial["candidate_id"],
                            "validation_id": trial["validation_id"],
                            "seed": trial["seed"],
                            "metric": metric,
                            "value": "",
                            "status": "missing",
                            "artifact_path": "",
                            "error": "",
                            "actual_iterations": "",
                        }
                    )
        write_csv(args.output_dir / "all_results.csv", list(all_rows[0]), all_rows)

        trial_ids_by_candidate: dict[str, set[str]] = defaultdict(set)
        for trial in trials:
            trial_ids_by_candidate[trial["candidate_id"]].add(trial["trial_id"])
        completeness_rows: list[dict] = []
        complete_candidates: set[str] = set()
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            expected = trial_ids_by_candidate[candidate_id]
            completed_ids = expected & set(completed)
            failed_ids = expected & set(failed)
            missing_ids = expected - completed_ids - failed_ids
            is_complete = len(completed_ids) == len(expected)
            if is_complete:
                complete_candidates.add(candidate_id)
            completeness_rows.append(
                {
                    "candidate_id": candidate_id,
                    "expected_trials": len(expected),
                    "completed_trials": len(completed_ids),
                    "failed_trials": len(failed_ids),
                    "missing_trials": len(missing_ids),
                    "complete": str(is_complete).lower(),
                    "reason": "" if is_complete else "failed_or_missing_trials",
                }
            )
        write_csv(
            args.output_dir / "completeness.csv",
            ["candidate_id", "expected_trials", "completed_trials", "failed_trials", "missing_trials", "complete", "reason"],
            completeness_rows,
        )

        values_by_candidate_validation_metric: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for trial in trials:
            state = completed.get(trial["trial_id"])
            if state is None:
                continue
            for metric, value in state["metrics"].items():
                values_by_candidate_validation_metric[(trial["candidate_id"], trial["validation_id"], metric)].append(value)

        validation_ids = [item["id"] for item in config["development"]["validation_periods"]]
        validation_rows: list[dict] = []
        validation_means: dict[tuple[str, str], list[float]] = defaultdict(list)
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            for validation_id in validation_ids:
                for metric in metric_names:
                    values = values_by_candidate_validation_metric[(candidate_id, validation_id, metric)]
                    mean_value = mean_or_none(values)
                    if mean_value is not None:
                        validation_means[(candidate_id, metric)].append(mean_value)
                    validation_rows.append(
                        {
                            "candidate_id": candidate_id,
                            "validation_id": validation_id,
                            "metric": metric,
                            "expected_seeds": len(config["seeds"]),
                            "completed_seeds": len(values),
                            "mean": format_number(mean_value),
                            "minimum": format_number(min(values) if values else None),
                            "maximum": format_number(max(values) if values else None),
                            "stdev": format_number(stdev_or_none(values)),
                        }
                    )
        write_csv(
            args.output_dir / "validation_summary.csv",
            ["candidate_id", "validation_id", "metric", "expected_seeds", "completed_seeds", "mean", "minimum", "maximum", "stdev"],
            validation_rows,
        )

        candidate_rows: list[dict] = []
        candidate_metric_means: dict[str, dict[str, float]] = defaultdict(dict)
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            for metric in metric_names:
                values = validation_means[(candidate_id, metric)]
                mean_value = mean_or_none(values)
                if mean_value is not None:
                    candidate_metric_means[candidate_id][metric] = mean_value
                worst = None
                if values:
                    worst = min(values) if metric_directions[metric] == "max" else max(values)
                candidate_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "metric": metric,
                        "direction": metric_directions[metric],
                        "completed_validation_periods": len(values),
                        "expected_validation_periods": len(validation_ids),
                        "mean": format_number(mean_value),
                        "median": format_number(statistics.median(values) if values else None),
                        "worst_period": format_number(worst),
                        "stdev": format_number(stdev_or_none(values)),
                        "complete": str(candidate_id in complete_candidates).lower(),
                    }
                )
        write_csv(
            args.output_dir / "candidate_summary.csv",
            ["candidate_id", "metric", "direction", "completed_validation_periods", "expected_validation_periods", "mean", "median", "worst_period", "stdev", "complete"],
            candidate_rows,
        )

        ranking_rows: list[dict] = []
        for metric in metrics:
            name = metric["name"]
            eligible = [
                (candidate_id, values[name])
                for candidate_id, values in candidate_metric_means.items()
                if candidate_id in complete_candidates and name in values
            ]
            eligible.sort(key=lambda item: ((-item[1]) if metric["direction"] == "max" else item[1], item[0]))
            for rank, (candidate_id, value) in enumerate(eligible, start=1):
                ranking_rows.append(
                    {"metric": name, "direction": metric["direction"], "rank": rank, "candidate_id": candidate_id, "mean": format_number(value)}
                )
        write_csv(args.output_dir / "metric_rankings.csv", ["metric", "direction", "rank", "candidate_id", "mean"], ranking_rows)

        pareto_input = {
            candidate_id: values
            for candidate_id, values in candidate_metric_means.items()
            if candidate_id in complete_candidates and set(values) == set(metric_names)
        }
        pareto_candidate_ids = pareto_ids(pareto_input, metrics)
        config_by_candidate = {row["candidate_id"]: row["config"] for row in candidates}
        pareto_rows: list[dict] = []
        for candidate_id in pareto_candidate_ids:
            row = {"candidate_id": candidate_id}
            row.update({metric: format_number(pareto_input[candidate_id][metric]) for metric in metric_names})
            row["candidate_config_json"] = canonical_json(config_by_candidate[candidate_id])
            pareto_rows.append(row)
        pareto_fields = ["candidate_id", *metric_names, "candidate_config_json"]
        write_csv(args.output_dir / "pareto_candidates.csv", pareto_fields, pareto_rows)

        report = render_report(manifest, metrics, completeness_rows, ranking_rows, pareto_rows)
        (args.output_dir / "report.md").write_text(report, encoding="utf-8")
        summary_manifest = {
            "schema_version": 1,
            "study_id": config["study_id"],
            "source_plan_sha256": file_sha256(args.plan_dir / "study_manifest.json"),
            "source_results_sha256": file_sha256(args.results),
            "complete_candidate_count": len(complete_candidates),
            "pareto_candidate_count": len(pareto_rows),
            "automatic_winner_selected": False,
            "configuration_frozen": False,
        }
        (args.output_dir / "summary_manifest.json").write_text(
            json.dumps(summary_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"summarized {len(candidates)} candidates; complete={len(complete_candidates)}, "
            f"pareto={len(pareto_rows)} in {args.output_dir}"
        )
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

