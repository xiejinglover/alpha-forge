#!/usr/bin/env python3
"""Build a conclusion-first Markdown report from frozen portfolio artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

from portfolio_optimization_core import ContractError, SCHEMA_VERSION, object_hash, sha256_file


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _pct(value: str) -> str:
    number = _number(value)
    return "—" if not math.isfinite(number) else f"{number:.2%}"


def _num(value: str) -> str:
    number = _number(value)
    return "—" if not math.isfinite(number) else f"{number:.3f}"


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("stage") != "portfolio_optimization_complete":
        raise ContractError("unsupported portfolio manifest")
    run_hash = manifest.get("run_hash")
    without_hash = {key: value for key, value in manifest.items() if key != "run_hash"}
    if run_hash != object_hash(without_hash):
        raise ContractError("frozen_spec.json hash is invalid")
    for filename, expected in manifest["artifacts"].items():
        artifact = path.parent / filename
        if not artifact.exists() or sha256_file(artifact) != expected:
            raise ContractError(f"frozen artifact changed: {artifact}")
    return manifest


def render_report(manifest: dict[str, Any], root: Path) -> str:
    metrics = _read_csv(root / "portfolio_metrics.csv")
    candidates = _read_csv(root / "candidate_risk_metrics.csv")
    infeasible = _read_csv(root / "infeasible.csv")
    ledger = _read_csv(root / "oos_access_ledger.csv")
    primary = {str(item["deployment_id"]): str(item["benchmark_id"]) for item in manifest["study"]["deployments"]}
    primary["ALL_ROLLING"] = str(manifest["study"]["deployments"][0]["benchmark_id"])
    evaluation = [
        row for row in metrics
        if row["period"] in {"evaluation", "rolling_evaluation"}
        and row["benchmark_id"] == primary[row["deployment_id"]]
    ]
    current_ledger = [row for row in ledger if row["deployment_id"]]
    consumed_only = bool(current_ledger) and all(
        row["used_for_selection"] == "true" or row["evidence_role"] == "consumed_research"
        for row in current_ledger
    )
    positive_alpha = sum(1 for row in evaluation if _number(row["residual_alpha"]) > 0)
    lines = [
        "# 策略组合优化审计报告", "",
        "## 总体结论", "",
        "- 证据结论：`__VERDICT__`。",
        f"- 冻结评估配置数：{len(evaluation)}；其中控制后 Alpha 为正：{positive_alpha}。",
        "- 低 Beta 只在质量保护后的正 Alpha 候选池内具有研究意义；Beta 下降本身不证明存在独立 Alpha。",
        "- 本报告的线性袖套不等于股票投票或联合资金账户。", "",
        "## 冻结评估结果", "",
        "| 批次 | 方案 | N | 年化 | Sharpe | 最大回撤 | 普通Beta | 控制后Beta | 下行Beta | 尾部10%Beta | 年化残差Alpha |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(evaluation, key=lambda item: (item["deployment_id"], int(item["n"]), item["scheme"])):
        lines.append(
            f"| {row['deployment_id']} | {row['scheme']} | {row['n']} | {_pct(row['annual_return'])} | "
            f"{_num(row['sharpe'])} | {_pct(row['max_drawdown'])} | {_num(row['ordinary_beta'])} | "
            f"{_num(row['controlled_beta'])} | {_num(row['downside_beta'])} | {_num(row['tail_10_beta'])} | "
            f"{_pct(row['residual_alpha'])} |"
        )

    lines.extend(["", "## 低 Beta 相对质量对照", ""])
    lookup = {(row["deployment_id"], row["scheme"], row["n"]): row for row in evaluation}
    comparisons = []
    for key, low in sorted(lookup.items()):
        deployment_id, scheme, n = key
        if scheme != "LOW_BETA_EQ":
            continue
        quality = lookup.get((deployment_id, "QUALITY_EQ", n))
        if quality is None:
            continue
        quality_return = _number(quality["annual_return"])
        low_return = _number(low["annual_return"])
        retention = low_return / quality_return if quality_return > 0 else math.nan
        ordinary_reduction = (
            1 - _number(low["ordinary_beta"]) / _number(quality["ordinary_beta"])
            if _number(quality["ordinary_beta"]) != 0 else math.nan
        )
        downside_reduction = (
            1 - _number(low["downside_beta"]) / _number(quality["downside_beta"])
            if _number(quality["downside_beta"]) != 0 else math.nan
        )
        tail_reduction = (
            1 - _number(low["tail_10_beta"]) / _number(quality["tail_10_beta"])
            if _number(quality["tail_10_beta"]) != 0 else math.nan
        )
        sharpe_delta = _number(low["sharpe"]) - _number(quality["sharpe"])
        drawdown_delta = _number(low["max_drawdown"]) - _number(quality["max_drawdown"])
        if low_return <= 0 or _number(low["residual_alpha"]) <= 0:
            reading = "Beta低但Alpha/收益不足"
        elif ordinary_reduction > 0 and downside_reduction >= 0 and tail_reduction >= 0:
            reading = "三类暴露同向下降"
        elif ordinary_reduction > 0:
            reading = "仅部分风险改善"
        else:
            reading = "普通Beta未改善"
        comparisons.append((
            deployment_id, n, retention, ordinary_reduction, sharpe_delta, drawdown_delta,
            downside_reduction, tail_reduction, reading, _number(low["residual_alpha"]),
        ))
    if comparisons:
        lines.extend([
            "| 批次 | N | 收益保留率 | Sharpe差 | 回撤差 | 普通Beta降幅 | 下行Beta降幅 | 尾部10%Beta降幅 | 判读 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for deployment_id, n, retention, reduction, sharpe_delta, drawdown_delta, downside_reduction, tail_reduction, reading, _ in comparisons:
            retention_text = "—" if not math.isfinite(retention) else f"{retention:.2%}"
            reduction_text = "—" if not math.isfinite(reduction) else f"{reduction:.2%}"
            downside_text = "—" if not math.isfinite(downside_reduction) else f"{downside_reduction:.2%}"
            tail_text = "—" if not math.isfinite(tail_reduction) else f"{tail_reduction:.2%}"
            lines.append(
                f"| {deployment_id} | {n} | {retention_text} | {sharpe_delta:.3f} | "
                f"{drawdown_delta:.2%} | {reduction_text} | {downside_text} | {tail_text} | {reading} |"
            )
        rolling_comparisons = [row for row in comparisons if row[0] != "ALL_ROLLING"]
        aligned = sum(
            1 for row in rolling_comparisons
            if row[3] > 0 and row[6] >= 0 and row[7] >= 0
        )
        positive = sum(1 for row in rolling_comparisons if "Alpha/收益不足" not in row[8])
        lines.extend([
            "",
            f"- 逐期/N配对数：{len(rolling_comparisons)}；普通、下行和尾部Beta同向不恶化：{aligned}。",
            f"- 低Beta组合在冻结评估期保持正收益且正残差Alpha：{positive}/{len(rolling_comparisons)}。",
            "- 若多数配置不满足同向风险改善或正Alpha，应优先解释为暴露转移、弱交易或Alpha消失，不得只根据普通Beta宣称优化成功。",
        ])
    else:
        lines.append("无可配对的 `QUALITY_EQ` 与 `LOW_BETA_EQ` 评估结果。")

    lines.extend([
        "", "## 候选分母与不可行项", "",
        f"- 候选诊断行：{len(candidates)}。",
        f"- 通过全部资格门禁：{sum(row['eligible'] == 'True' for row in candidates)}。",
        f"- 进入质量保护池：{sum(row['in_quality_pool'] == 'True' for row in candidates)}。",
        f"- 不可行记录：{len(infeasible)}。", "",
    ])
    if infeasible:
        lines.extend(["| 批次 | 方案 | N | 阶段 | 原因 |", "|---|---|---:|---|---|"])
        for row in infeasible:
            lines.append(
                f"| {row['deployment_id']} | {row['scheme']} | {row['n']} | {row['stage']} | {row['reason']} |"
            )

    lines.extend([
        "", "## 证据边界", "",
        "- 所有成员选择、Beta、残差相关和簇约束只使用各批次估计期。",
        "- 任何已用于修改 N、门槛或方案的评估期都必须保持已消费身份。",
        "- 未预注册结论阈值时，本报告只提供方向性证据，不自动签发生产许可。",
        "- 详细成员、日收益、相关性、排除原因和输入哈希见同目录结构化产物。", "",
        f"`research_hash`: `{manifest['research_hash']}`", "",
    ])
    verdict = "diagnostic_only" if consumed_only else "research_candidate_pending_forward"
    rules = manifest["study"].get("decision_rules")
    if (
        not consumed_only and rules and current_ledger
        and all(row["evidence_role"] == "forward_monitoring" for row in current_ledger)
    ):
        assessable = [row for row in comparisons if row[0] != "ALL_ROLLING"]
        passed = [
            row for row in assessable
            if row[2] >= float(rules["minimum_return_retention"])
            and row[4] >= float(rules["minimum_sharpe_delta"])
            and row[5] >= -float(rules["maximum_drawdown_worsening"])
            and row[3] >= float(rules["minimum_ordinary_beta_reduction"])
            and row[6] >= float(rules["minimum_downside_beta_reduction"])
            and row[7] >= float(rules["minimum_tail10_beta_reduction"])
            and row[9] >= float(rules["minimum_residual_alpha"])
        ]
        if assessable and len(passed) / len(assessable) >= float(rules["minimum_config_pass_rate"]):
            verdict = "forward_supported"
    return "\n".join(lines).replace("__VERDICT__", verdict)


def build_report(manifest_path: Path, output_path: Path) -> None:
    if output_path.exists():
        raise ContractError(f"report output already exists: {output_path}")
    manifest = _load_manifest(manifest_path)
    output_path.write_text(render_report(manifest, manifest_path.parent), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        build_report(args.manifest, args.output)
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
