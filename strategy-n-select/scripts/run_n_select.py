#!/usr/bin/env python3
"""Run deterministic N-selection and frozen-member multi-asset voting."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from n_select_core import (
    ContractError,
    DEFAULT_MATCHES,
    DEFAULT_N_VALUES,
    DEFAULT_SEED,
    SCHEMA_VERSION,
    load_study_config,
    object_hash,
    parse_iso_date,
    parse_positive_ints,
    prepare_output_dir,
    read_development_signals,
    read_member_records,
    read_members,
    read_scores,
    read_signals,
    run_decorrelation,
    run_selection,
    run_voting,
    sha256_file,
    verify_weight_sums,
    write_csv,
    write_json,
)


GROUP_FIELDS = ["n", "round", "position", "candidate_id"]
WINNER_FIELDS = ["n", "round", "status", "winner_candidate_id", "winner_score"]
MEMBER_FIELDS = ["n", "candidate_id", "metric_score", "win_count", "slot_weight"]
SIMILARITY_FIELDS = [
    "n",
    "candidate_id_a",
    "candidate_id_b",
    "common_rebalance_dates",
    "similarity",
]
DECORRELATION_TRACE_FIELDS = [
    "n",
    "order",
    "candidate_id",
    "metric_score",
    "win_count",
    "max_similarity_to_kept",
    "blocking_candidate_id",
    "decision",
]
DECORRELATION_SUMMARY_FIELDS = [
    "n",
    "members_before",
    "members_after",
    "members_removed",
    "slots_before",
    "slots_after",
    "slots_removed",
    "max_retained_similarity",
]
MEMBER_VOTE_FIELDS = [
    "rebalance_date",
    "n",
    "member_vote_mode",
    "candidate_id",
    "asset_id",
    "member_asset_count",
    "vote_weight",
]
MISSING_SIGNAL_FIELDS = ["rebalance_date", "n", "member_vote_mode", "candidate_id"]
ASSET_VOTE_FIELDS = ["rebalance_date", "n", "member_vote_mode", "asset_id", "votes", "rank"]
TARGET_WEIGHT_FIELDS = [
    "rebalance_date",
    "n",
    "member_vote_mode",
    "asset_selection_mode",
    "top_k",
    "selection_label",
    "asset_id",
    "votes",
    "target_weight",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select", help="Run N-selection using development-period scores")
    select.add_argument("--scores", required=True, type=Path)
    select.add_argument("--study-config", required=True, type=Path)
    select.add_argument("--metric", required=True)
    select.add_argument("--direction", choices=("max", "min"), required=True)
    select.add_argument("--n-values", default=",".join(str(value) for value in DEFAULT_N_VALUES))
    select.add_argument("--matches", type=int, default=DEFAULT_MATCHES)
    select.add_argument("--seed", type=int, default=DEFAULT_SEED)
    select.add_argument("--output-dir", required=True, type=Path)

    decorrelate = subparsers.add_parser(
        "decorrelate", help="Build the mandatory member pool using development signal overlap"
    )
    decorrelate.add_argument("--selection-manifest", required=True, type=Path)
    decorrelate.add_argument("--development-signals", required=True, type=Path)
    decorrelate.add_argument("--threshold", type=float, default=0.6)
    decorrelate.add_argument("--output-dir", required=True, type=Path)

    vote = subparsers.add_parser("vote", help="Generate holdout rebalance targets from frozen members")
    vote.add_argument("--decorrelation-manifest", required=True, type=Path)
    vote.add_argument("--signals", required=True, type=Path)
    vote.add_argument("--top-k", default="1,3,5")
    vote.add_argument("--output-dir", required=True, type=Path)
    return parser


def _select(args: argparse.Namespace) -> None:
    study = load_study_config(args.study_config)
    n_values = parse_positive_ints(args.n_values, "n-values")
    if args.matches <= 0:
        raise ContractError("matches must be positive")
    scores = read_scores(args.scores, args.metric)
    prepare_output_dir(args.output_dir)
    groups, winners, members = run_selection(
        scores=scores,
        n_values=n_values,
        matches=args.matches,
        seed=args.seed,
        direction=args.direction,
    )

    groups_path = args.output_dir / "groups.csv"
    winners_path = args.output_dir / "winners.csv"
    members_path = args.output_dir / "members.csv"
    write_csv(groups_path, GROUP_FIELDS, groups)
    write_csv(winners_path, WINNER_FIELDS, winners)
    write_csv(members_path, MEMBER_FIELDS, members)

    effective = {
        "n_values": n_values,
        "matches": args.matches,
        "seed": args.seed,
        "metric": args.metric,
        "direction": args.direction,
        "rng": "python.random.Random",
        "tie_break": "candidate_id_ascending",
    }
    defaults = {
        "n_values": DEFAULT_N_VALUES,
        "matches": DEFAULT_MATCHES,
        "seed": DEFAULT_SEED,
    }
    overrides = {
        key: effective[key] != defaults[key]
        for key in ("n_values", "matches", "seed")
    }
    manifest_without_hash = {
        "schema_version": SCHEMA_VERSION,
        "stage": "selection_frozen",
        "study": study,
        "holdout_evidence_role": (
            "diagnostic_only" if study["holdout"]["consumed_for_selection"] else "independent_holdout"
        ),
        "defaults": defaults,
        "effective": effective,
        "overrides": overrides,
        "inputs": {
            "scores": {"sha256": sha256_file(args.scores)},
            "study_config": {"sha256": sha256_file(args.study_config)},
        },
        "artifacts": {
            "groups.csv": sha256_file(groups_path),
            "winners.csv": sha256_file(winners_path),
            "members.csv": sha256_file(members_path),
        },
        "summary": {
            "candidate_count": len(scores),
            "match_rows": len(winners),
            "valid_winner_rows": sum(1 for row in winners if row["status"] == "winner"),
            "invalid_group_rows": sum(1 for row in winners if row["status"] == "invalid_group"),
            "unique_member_rows": len(members),
        },
    }
    manifest = {**manifest_without_hash, "selection_hash": object_hash(manifest_without_hash)}
    write_json(args.output_dir / "selection_manifest.json", manifest)
    summary = (
        "# N 选优选择阶段\n\n"
        f"- 指标：`{args.metric}`（`{args.direction}`）\n"
        f"- N：`{n_values}`\n"
        f"- 每档场数：`{args.matches}`\n"
        f"- 随机种子：`{args.seed}`\n"
        f"- 候选数：`{len(scores)}`\n"
        f"- 有效赢家场：`{manifest['summary']['valid_winner_rows']}`\n"
        f"- 异常空场：`{manifest['summary']['invalid_group_rows']}`\n"
        f"- 留出证据角色：`{manifest['holdout_evidence_role']}`\n"
        f"- 冻结哈希：`{manifest['selection_hash']}`\n"
    )
    (args.output_dir / "summary.md").write_text(summary, encoding="utf-8")


def _load_and_verify_selection_manifest(path: Path) -> tuple[dict, Path]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read valid selection manifest {path}: {exc}") from exc
    if manifest.get("stage") != "selection_frozen" or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("Unsupported or unfrozen selection manifest")
    selection_hash = manifest.get("selection_hash")
    manifest_without_hash = {key: value for key, value in manifest.items() if key != "selection_hash"}
    if selection_hash != object_hash(manifest_without_hash):
        raise ContractError("selection_manifest.json hash is invalid")
    selection_dir = path.parent
    for filename, expected_hash in manifest.get("artifacts", {}).items():
        artifact_path = selection_dir / filename
        if not artifact_path.exists() or sha256_file(artifact_path) != expected_hash:
            raise ContractError(f"Frozen selection artifact changed: {artifact_path}")
    return manifest, selection_dir


def _decorrelate(args: argparse.Namespace) -> None:
    selection_manifest, selection_dir = _load_and_verify_selection_manifest(args.selection_manifest)
    study = selection_manifest["study"]
    development_start = parse_iso_date(study["development"]["start"], "development.start")
    development_end = parse_iso_date(study["development"]["end"], "development.end")
    members_by_n = read_member_records(selection_dir / "members.csv")
    required_candidates = {
        record["candidate_id"]
        for records in members_by_n.values()
        for record in records
    }
    signals_by_candidate = read_development_signals(
        args.development_signals,
        development_start,
        development_end,
        required_candidates,
    )
    prepare_output_dir(args.output_dir)
    similarities, trace, decorrelated_members, summaries = run_decorrelation(
        members_by_n=members_by_n,
        signals_by_candidate=signals_by_candidate,
        direction=selection_manifest["effective"]["direction"],
        threshold=args.threshold,
    )
    if not decorrelated_members:
        raise ContractError("Decorrelation produced an empty member pool")

    artifact_rows = {
        "similarities.csv": (SIMILARITY_FIELDS, similarities),
        "decorrelation_trace.csv": (DECORRELATION_TRACE_FIELDS, trace),
        "decorrelated_members.csv": (MEMBER_FIELDS, decorrelated_members),
        "decorrelation_summary.csv": (DECORRELATION_SUMMARY_FIELDS, summaries),
    }
    for filename, (fieldnames, rows) in artifact_rows.items():
        write_csv(args.output_dir / filename, fieldnames, rows)

    manifest_without_hash = {
        "schema_version": SCHEMA_VERSION,
        "stage": "decorrelation_frozen",
        "study": study,
        "holdout_evidence_role": selection_manifest["holdout_evidence_role"],
        "selection_hash": selection_manifest["selection_hash"],
        "effective": {
            "metric": selection_manifest["effective"]["metric"],
            "direction": selection_manifest["effective"]["direction"],
            "similarity": "mean_development_rebalance_asset_overlap_fraction",
            "threshold": args.threshold,
            "threshold_operator": "remove_if_strictly_greater",
            "priority": "metric_score_then_candidate_id",
        },
        "inputs": {
            "development_signals": {"sha256": sha256_file(args.development_signals)},
        },
        "artifacts": {
            filename: sha256_file(args.output_dir / filename)
            for filename in artifact_rows
        },
        "summary": summaries,
    }
    manifest = {**manifest_without_hash, "decorrelation_hash": object_hash(manifest_without_hash)}
    write_json(args.output_dir / "decorrelation_manifest.json", manifest)
    summary_lines = [
        "# N 选优策略去相关",
        "",
        f"- 指标：`{manifest['effective']['metric']}`（`{manifest['effective']['direction']}`）",
        f"- 相似度：开发期逐调仓日选标重合率的平均值",
        f"- 删除规则：`similarity > {args.threshold}`",
        f"- 选择冻结哈希：`{manifest['selection_hash']}`",
        f"- 去相关冻结哈希：`{manifest['decorrelation_hash']}`",
        "",
        "| N | 去相关前 | 去相关后 | 删除成员 | 删除席位 | 新池最大相似度 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    summary_lines.extend(
        f"| {row['n']} | {row['members_before']} | {row['members_after']} | "
        f"{row['members_removed']} | {row['slots_removed']} | {row['max_retained_similarity']} |"
        for row in summaries
    )
    (args.output_dir / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def _load_and_verify_decorrelation_manifest(path: Path) -> tuple[dict, Path]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read valid decorrelation manifest {path}: {exc}") from exc
    if manifest.get("stage") != "decorrelation_frozen" or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("Unsupported or unfrozen decorrelation manifest")
    decorrelation_hash = manifest.get("decorrelation_hash")
    manifest_without_hash = {key: value for key, value in manifest.items() if key != "decorrelation_hash"}
    if decorrelation_hash != object_hash(manifest_without_hash):
        raise ContractError("decorrelation_manifest.json hash is invalid")
    decorrelation_dir = path.parent
    for filename, expected_hash in manifest.get("artifacts", {}).items():
        artifact_path = decorrelation_dir / filename
        if not artifact_path.exists() or sha256_file(artifact_path) != expected_hash:
            raise ContractError(f"Frozen decorrelation artifact changed: {artifact_path}")
    return manifest, decorrelation_dir


def _vote(args: argparse.Namespace) -> None:
    manifest, decorrelation_dir = _load_and_verify_decorrelation_manifest(
        args.decorrelation_manifest
    )
    top_k_values = parse_positive_ints(args.top_k, "top-k")
    holdout = manifest["study"]["holdout"]
    holdout_start = parse_iso_date(holdout["start"], "holdout.start")
    holdout_end = parse_iso_date(holdout["end"], "holdout.end")
    members_by_n = read_members(decorrelation_dir / "decorrelated_members.csv")
    signals_by_date, ignored_outside_holdout = read_signals(args.signals, holdout_start, holdout_end)
    prepare_output_dir(args.output_dir)
    member_votes, missing_signals, asset_votes, target_weights = run_voting(
        members_by_n=members_by_n,
        signals_by_date=signals_by_date,
        top_k_values=top_k_values,
    )
    if not target_weights:
        raise ContractError("No target weights were generated; all winner signals are missing")
    verify_weight_sums(target_weights)

    paths = {
        "member_votes.csv": (MEMBER_VOTE_FIELDS, member_votes),
        "missing_signals.csv": (MISSING_SIGNAL_FIELDS, missing_signals),
        "asset_votes.csv": (ASSET_VOTE_FIELDS, asset_votes),
        "target_weights.csv": (TARGET_WEIGHT_FIELDS, target_weights),
    }
    for filename, (fieldnames, rows) in paths.items():
        write_csv(args.output_dir / filename, fieldnames, rows)

    portfolio_without_hash = {
        "schema_version": SCHEMA_VERSION,
        "stage": "portfolio_targets_frozen",
        "selection_hash": manifest["selection_hash"],
        "decorrelation_hash": manifest["decorrelation_hash"],
        "holdout_evidence_role": manifest["holdout_evidence_role"],
        "member_vote_modes": ["slot_weighted", "unique_equal"],
        "member_signal_allocation": "full_member_vote_for_each_emitted_asset",
        "asset_selection": {"modes": ["all", "top_k"], "top_k_values": top_k_values},
        "capital_weighting": "selected_asset_vote_share",
        "inputs": {"signals": {"sha256": sha256_file(args.signals)}},
        "artifacts": {
            filename: sha256_file(args.output_dir / filename)
            for filename in paths
        },
        "summary": {
            "rebalance_dates": len(signals_by_date),
            "ignored_signal_rows_outside_holdout": ignored_outside_holdout,
            "member_vote_rows": len(member_votes),
            "missing_signal_rows": len(missing_signals),
            "asset_vote_rows": len(asset_votes),
            "target_weight_rows": len(target_weights),
        },
    }
    portfolio = {**portfolio_without_hash, "portfolio_hash": object_hash(portfolio_without_hash)}
    write_json(args.output_dir / "portfolio_manifest.json", portfolio)
    summary = (
        "# N 选优投票组合\n\n"
        f"- 选择冻结哈希：`{manifest['selection_hash']}`\n"
        f"- 去相关冻结哈希：`{manifest['decorrelation_hash']}`\n"
        f"- 成员投票：`slot_weighted`、`unique_equal`\n"
        "- 成员信号：保留原策略选标数量，每只标的获得完整成员票\n"
        f"- 标的选择：`all`、`top_k={top_k_values}`\n"
        "- 资金权重：入选标的票数占比\n"
        f"- 调仓日数：`{len(signals_by_date)}`\n"
        f"- 缺失成员信号行：`{len(missing_signals)}`\n"
        f"- 留出证据角色：`{manifest['holdout_evidence_role']}`\n"
        f"- 组合冻结哈希：`{portfolio['portfolio_hash']}`\n\n"
        "将 `target_weights.csv` 交给目标仓库原有执行层；非调仓日沿用上一目标持仓。\n"
    )
    (args.output_dir / "summary.md").write_text(summary, encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "select":
            _select(args)
        elif args.command == "decorrelate":
            _decorrelate(args)
        else:
            _vote(args)
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
