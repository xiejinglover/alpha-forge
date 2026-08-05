#!/usr/bin/env python3
"""Validate a structured ML overfitting audit and render a Markdown report.

The input contract is documented in references/audit-execution-and-report.md.
This script does not run strategy experiments; it only prevents incomplete
audit evidence from being silently collapsed into a short verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


EXPECTED_MODULES = (
    "ML-001",
    "ML-002",
    "ML-003",
    "ML-004",
    "ML-005",
    "ML-007",
    "ML-008",
    "ML-009",
    "ML-010",
    "ML-011",
)
EXPECTED_PHASES = (
    "repository_discovery",
    "baseline_reproduction",
    "experiment_planning",
    "experiment_execution",
    "evidence_aggregation",
    "final_assessment",
)
MODULE_FIELDS = (
    "module_id",
    "title",
    "role",
    "priority",
    "execution_mode",
    "execution_status",
    "blocked_reason_type",
    "verdict",
    "question",
    "hypothesis",
    "repository_evidence",
    "reused_code",
    "commands",
    "changed_variables",
    "controlled_variables",
    "cases",
    "data_scope",
    "thresholds",
    "metrics",
    "artifacts",
    "facts",
    "interpretation",
    "limitations",
    "skip_reason",
    "user_input_needed",
    "selection_impact",
)
VERDICTS = {"PASSED", "FAILED", "BLOCKED", "INCONCLUSIVE", "NOT_APPLICABLE"}
EXECUTION_MODES = {
    "REUSE_DIRECT",
    "REUSE_WITH_OVERRIDE",
    "NO_SAFE_REUSE_PATH",
    "NOT_APPLICABLE",
}
EXECUTION_STATUSES = {
    "COMPLETED",
    "PARTIAL",
    "NEEDS_USER_INPUT",
    "SKIPPED_UNAVAILABLE",
    "NOT_APPLICABLE",
    "FAILED_TO_RUN",
}
BLOCKED_REASON_TYPES = {
    "MISSING_USER_DECISION",
    "EXECUTION_NOT_AUTHORIZED",
    "MISSING_REPOSITORY_CAPABILITY",
    "MISSING_DATA_OR_ENVIRONMENT",
}
PHASE_STATUSES = {"COMPLETED", "PARTIAL", "BLOCKED", "SKIPPED", "NOT_STARTED"}
PHASE_LABELS = {
    "repository_discovery": "仓库与执行入口检查",
    "baseline_reproduction": "基线复现",
    "experiment_planning": "实验规划",
    "experiment_execution": "实验执行",
    "evidence_aggregation": "证据汇总",
    "final_assessment": "综合判定",
}


class ManifestError(ValueError):
    """Raised when the audit manifest cannot support a complete report."""


def _require_mapping(value: Any, path: str, errors: list[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return {}
    return value


def _require_list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{path} must be an array")
        return []
    return value


def _missing_keys(value: Mapping[str, Any], required: Sequence[str], path: str, errors: list[str]) -> None:
    missing = [key for key in required if key not in value]
    if missing:
        errors.append(f"{path} missing fields: {', '.join(missing)}")


def validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Return every completeness error found in an audit manifest."""

    errors: list[str] = []
    _missing_keys(
        manifest,
        (
            "report",
            "overall",
            "repository_execution_map",
            "baseline",
            "phase_log",
            "modules",
            "oos_access_ledger",
            "selection_record",
            "command_log",
            "file_changes",
            "errors",
        ),
        "manifest",
        errors,
    )

    report = _require_mapping(manifest.get("report"), "report", errors)
    _missing_keys(report, ("title", "generated_at", "repository", "strategy", "environment", "scope"), "report", errors)
    repository = _require_mapping(report.get("repository"), "report.repository", errors)
    _missing_keys(repository, ("path", "remote", "commit", "branch", "dirty"), "report.repository", errors)

    overall = _require_mapping(manifest.get("overall"), "overall", errors)
    _missing_keys(
        overall,
        (
            "eligibility_verdict",
            "overfitting_verdict",
            "executive_conclusion",
            "critical_findings",
            "limitations",
            "recommendations",
        ),
        "overall",
        errors,
    )
    for key in ("eligibility_verdict", "overfitting_verdict"):
        if overall.get(key) not in VERDICTS - {"NOT_APPLICABLE"}:
            errors.append(f"overall.{key} has invalid verdict: {overall.get(key)!r}")

    execution_map = _require_list(manifest.get("repository_execution_map"), "repository_execution_map", errors)
    if not execution_map:
        errors.append("repository_execution_map must contain discovered repository capabilities")
    for index, entry in enumerate(execution_map):
        item = _require_mapping(entry, f"repository_execution_map[{index}]", errors)
        _missing_keys(item, ("capability", "paths", "symbols", "command", "config_paths", "inputs", "outputs", "evidence"), f"repository_execution_map[{index}]", errors)

    baseline = _require_mapping(manifest.get("baseline"), "baseline", errors)
    _missing_keys(
        baseline,
        ("status", "command", "code_paths", "config", "data_window", "artifacts", "metrics", "historical_comparison", "conclusion"),
        "baseline",
        errors,
    )
    if not baseline.get("conclusion"):
        errors.append("baseline.conclusion must explain the reproduction result")

    phase_log = _require_list(manifest.get("phase_log"), "phase_log", errors)
    present_phases: set[str] = set()
    seen_steps: set[str] = set()
    for index, entry in enumerate(phase_log):
        item = _require_mapping(entry, f"phase_log[{index}]", errors)
        _missing_keys(item, ("step_id", "phase", "status", "objective", "action", "command", "inputs", "outputs", "result", "conclusion"), f"phase_log[{index}]", errors)
        step_id = str(item.get("step_id", ""))
        if step_id in seen_steps:
            errors.append(f"duplicate phase_log step_id: {step_id}")
        seen_steps.add(step_id)
        present_phases.add(str(item.get("phase", "")))
        if item.get("status") not in PHASE_STATUSES:
            errors.append(f"phase_log[{index}].status is invalid: {item.get('status')!r}")
        for key in ("objective", "action", "result", "conclusion"):
            if not item.get(key):
                errors.append(f"phase_log[{index}].{key} must be non-empty")
    missing_phases = [phase for phase in EXPECTED_PHASES if phase not in present_phases]
    if missing_phases:
        errors.append(f"phase_log missing phases: {', '.join(missing_phases)}")

    modules = _require_list(manifest.get("modules"), "modules", errors)
    module_ids = [str(item.get("module_id", "")) for item in modules if isinstance(item, Mapping)]
    counts = Counter(module_ids)
    missing_modules = [module_id for module_id in EXPECTED_MODULES if counts[module_id] == 0]
    duplicate_modules = [module_id for module_id, count in counts.items() if count > 1]
    unexpected_modules = [module_id for module_id in counts if module_id and module_id not in EXPECTED_MODULES]
    if missing_modules:
        errors.append(f"modules missing required entries: {', '.join(missing_modules)}")
    if duplicate_modules:
        errors.append(f"modules contain duplicates: {', '.join(duplicate_modules)}")
    if unexpected_modules:
        errors.append(f"modules contain unexpected ids: {', '.join(unexpected_modules)}")

    for index, entry in enumerate(modules):
        item = _require_mapping(entry, f"modules[{index}]", errors)
        module_id = item.get("module_id", f"index-{index}")
        path = f"modules[{module_id}]"
        _missing_keys(item, MODULE_FIELDS, path, errors)
        if item.get("execution_mode") not in EXECUTION_MODES:
            errors.append(f"{path}.execution_mode is invalid: {item.get('execution_mode')!r}")
        if item.get("execution_status") not in EXECUTION_STATUSES:
            errors.append(f"{path}.execution_status is invalid: {item.get('execution_status')!r}")
        if item.get("verdict") not in VERDICTS:
            errors.append(f"{path}.verdict is invalid: {item.get('verdict')!r}")
        blocked_reason_type = item.get("blocked_reason_type")
        if blocked_reason_type is not None and blocked_reason_type not in BLOCKED_REASON_TYPES:
            errors.append(f"{path}.blocked_reason_type is invalid: {blocked_reason_type!r}")
        for key in (
            "repository_evidence",
            "reused_code",
            "commands",
            "cases",
            "metrics",
            "artifacts",
            "facts",
            "interpretation",
            "limitations",
            "user_input_needed",
        ):
            _require_list(item.get(key), f"{path}.{key}", errors)

        status = item.get("execution_status")
        if status in {"COMPLETED", "PARTIAL"}:
            if not item.get("reused_code"):
                errors.append(f"{path} executed but reused_code is empty")
            if not item.get("facts"):
                errors.append(f"{path} executed but facts is empty")
            if not item.get("interpretation"):
                errors.append(f"{path} executed but interpretation is empty")
            if not item.get("commands") and not item.get("repository_evidence"):
                errors.append(f"{path} executed without commands or repository evidence")
        if status in {"SKIPPED_UNAVAILABLE", "FAILED_TO_RUN", "NEEDS_USER_INPUT"}:
            if not item.get("skip_reason"):
                errors.append(f"{path}.{status} requires skip_reason")
            if not item.get("repository_evidence"):
                errors.append(f"{path}.{status} requires repository_evidence showing what was checked")
            if item.get("verdict") == "PASSED":
                errors.append(f"{path}.{status} cannot have PASSED verdict")
        if status == "NEEDS_USER_INPUT" and blocked_reason_type not in {
            "MISSING_USER_DECISION",
            "EXECUTION_NOT_AUTHORIZED",
        }:
            errors.append(f"{path}.NEEDS_USER_INPUT requires a user-decision or authorization blocked_reason_type")
        if status == "SKIPPED_UNAVAILABLE":
            if item.get("execution_mode") != "NO_SAFE_REUSE_PATH":
                errors.append(f"{path}.SKIPPED_UNAVAILABLE requires NO_SAFE_REUSE_PATH execution_mode")
            if blocked_reason_type not in {
                "EXECUTION_NOT_AUTHORIZED",
                "MISSING_REPOSITORY_CAPABILITY",
                "MISSING_DATA_OR_ENVIRONMENT",
            }:
                errors.append(f"{path}.SKIPPED_UNAVAILABLE requires an unavailable-capability blocker type")
        if status == "NOT_APPLICABLE":
            if item.get("execution_mode") != "NOT_APPLICABLE" or item.get("verdict") != "NOT_APPLICABLE":
                errors.append(f"{path}.NOT_APPLICABLE requires matching execution_mode and verdict")
            if blocked_reason_type is not None:
                errors.append(f"{path}.NOT_APPLICABLE requires blocked_reason_type null")
        if status in {"COMPLETED", "PARTIAL"} and blocked_reason_type is not None:
            errors.append(f"{path}.{status} requires blocked_reason_type null")

    selection_record = _require_mapping(manifest.get("selection_record"), "selection_record", errors)
    _missing_keys(selection_record, ("dimensions", "post_selection_events", "consumed_oos_ids", "missing_facts"), "selection_record", errors)

    for key in ("oos_access_ledger", "command_log", "file_changes", "errors"):
        _require_list(manifest.get(key), key, errors)
    return errors


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", "<br>")


def _bullet_lines(values: Any, empty: str = "- 无") -> list[str]:
    if not values:
        return [empty]
    if isinstance(values, Mapping):
        return [f"- `{key}`：{_text(value)}" for key, value in values.items()]
    if isinstance(values, list):
        return [f"- {_text(value)}" for value in values]
    return [f"- {_text(values)}"]


def _json_block(value: Any) -> list[str]:
    return ["```json", json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), "```"]


def _summary(value: Any) -> str:
    if isinstance(value, list):
        return _text(value[0]) if value else "无"
    return _text(value)


def _compact_value(value: Any) -> str:
    if isinstance(value, list):
        if len(value) > 6:
            preview = "、".join(_text(item) for item in value[:3])
            return f"{preview} …（共 {len(value)} 项）"
        return "、".join(_text(item) for item in value) if value else "无"
    if isinstance(value, Mapping):
        return f"{len(value)} 个字段"
    return _text(value)


def _compact_mapping(value: Any) -> str:
    if not isinstance(value, Mapping) or not value:
        return "无"
    return "；".join(f"{key}={_compact_value(item)}" for key, item in value.items())


def _metric_table(metrics: Any, limit: int = 12) -> list[str]:
    if not metrics:
        return ["- 未产生可报告指标。"]
    rows = [
        "| Case | 证据用途 | 指标 | 结果 | 样本数 |",
        "|---|---|---|---:|---:|",
    ]
    for item in metrics[:limit]:
        if not isinstance(item, Mapping):
            rows.append(f"| — | — | — | {_cell(item)} | — |")
            continue
        value = item.get("value")
        if isinstance(value, float):
            value = f"{value:.6g}"
        unit = item.get("unit")
        result = f"{_text(value)}{(' ' + str(unit)) if unit else ''}"
        rows.append(
            "| " + " | ".join(
                _cell(part)
                for part in (
                    item.get("case_id"),
                    item.get("evidence_role"),
                    item.get("metric"),
                    result,
                    item.get("sample_count"),
                )
            ) + " |"
        )
    if len(metrics) > limit:
        rows.extend(["", f">其余 {len(metrics) - limit} 条指标保留在结构化审计底稿中。"])
    return rows


def _technical_appendix(manifest: Mapping[str, Any]) -> list[str]:
    lines = [
        "",
        "## 技术证据附录",
        "",
        ">本附录供复现和二次审查使用，默认报告不生成。",
        "",
        "### 仓库执行映射",
        "",
        *_json_block(manifest["repository_execution_map"]),
        "",
        "### 基线技术记录",
        "",
        *_json_block(manifest["baseline"]),
        "",
        "### 完整步骤日志",
        "",
        *_json_block(manifest["phase_log"]),
    ]
    for module_id in EXPECTED_MODULES:
        module = next(item for item in manifest["modules"] if item["module_id"] == module_id)
        technical = {
            "repository_evidence": module["repository_evidence"],
            "reused_code": module["reused_code"],
            "commands": module["commands"],
            "controlled_variables": module["controlled_variables"],
            "cases": module["cases"],
            "artifacts": module["artifacts"],
        }
        lines.extend(["", f"### {module_id} 技术底稿", "", *_json_block(technical)])
    lines.extend(
        [
            "",
            "### OOS 访问账本",
            "",
            *_json_block(manifest["oos_access_ledger"]),
            "",
            "### 选择记录",
            "",
            *_json_block(manifest["selection_record"]),
            "",
            "### 命令、文件变更与错误",
            "",
            *_json_block(
                {
                    "command_log": manifest["command_log"],
                    "file_changes": manifest["file_changes"],
                    "errors": manifest["errors"],
                }
            ),
        ]
    )
    return lines


def render_report(manifest: Mapping[str, Any], include_technical_appendix: bool = False) -> str:
    """Render a conclusion-first audit report; optionally append technical evidence."""

    errors = validate_manifest(manifest)
    if errors:
        raise ManifestError("\n".join(f"- {error}" for error in errors))

    report = manifest["report"]
    overall = manifest["overall"]
    repository = report["repository"]
    strategy = report["strategy"]
    baseline = manifest["baseline"]
    modules_by_id = {module["module_id"]: module for module in manifest["modules"]}
    status_counts = Counter(module["execution_status"] for module in manifest["modules"])

    lines = [
        f"# {report['title']}",
        "",
        "## 一、总体结论",
        "",
        f">{_text(overall['executive_conclusion'])}",
        "",
        "| 判断项 | 结论 |",
        "|---|---|",
        f"| 证据资格 | **{overall['eligibility_verdict']}** |",
        f"| 过拟合判断 | **{overall['overfitting_verdict']}** |",
        f"| 基线复现 | **{_cell(baseline['status'])}**：{_cell(baseline['conclusion'])} |",
        f"| 模块覆盖 | {_cell(_compact_mapping(dict(sorted(status_counts.items()))))} |",
        "",
        "### 最重要的发现",
        "",
        *_bullet_lines(overall["critical_findings"]),
        "",
        "### 当前结论的边界",
        "",
        *_bullet_lines(overall["limitations"]),
        "",
        "### 优先行动",
        "",
        *_bullet_lines(overall["recommendations"]),
        "",
        "## 二、审计对象与执行概览",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 策略 | {_cell(strategy.get('name'))} |",
        f"| 模型 | {_cell(strategy.get('model'))} |",
        f"| 市场/频率 | {_cell(strategy.get('market'))} / {_cell(strategy.get('frequency'))} |",
        f"| 标签与执行 | {_cell(strategy.get('label'))}；{_cell(strategy.get('decision_time'))} → {_cell(strategy.get('execution_time'))} |",
        f"| 代码版本 | {_cell(repository.get('commit'))} ({_cell(repository.get('branch'))}) |",
        f"| 数据范围 | {_cell(baseline.get('data_window'))} |",
        f"| 生成时间 | {_cell(report['generated_at'])} |",
        "",
        "### 审计过程概览",
        "",
        "| 阶段 | 状态 | 做了什么 | 阶段结论 |",
        "|---|---|---|---|",
    ]
    for step in manifest["phase_log"]:
        lines.append(
            f"| {_cell(PHASE_LABELS.get(step['phase'], step['phase']))} | {_cell(step['status'])} | {_cell(step['action'])} | {_cell(step['conclusion'])} |"
        )

    lines.extend(
        [
            "",
            "## 三、模块结论总览",
            "",
            "| 模块 | 状态 | 结论 | 一句话摘要 |",
            "|---|---|---|---|",
        ]
    )
    for module_id in EXPECTED_MODULES:
        module = modules_by_id[module_id]
        summary = module["interpretation"] or module["skip_reason"] or module["facts"]
        lines.append(
            f"| {module_id} {_cell(module['title'])} | {_cell(module['execution_status'])} | **{_cell(module['verdict'])}** | {_cell(_summary(summary))} |"
        )

    lines.extend(["", "## 四、各模块详细分析"])
    for module_id in EXPECTED_MODULES:
        module = modules_by_id[module_id]
        conclusion = module["interpretation"] or module["skip_reason"] or module["facts"]
        case_count = len(module["cases"])
        lines.extend(
            [
                "",
                f"### {module_id} {module['title']}",
                "",
                f">**{module['verdict']}** — {_summary(conclusion)}",
                "",
                "| 项目 | 内容 |",
                "|---|---|",
                f"| 检验目的 | {_cell(module['question'])} |",
                f"| 执行情况 | {_cell(module['execution_status'])}；{_cell(module['role'])} |",
                f"| 实验范围 | {case_count} 个 case；{_cell(_compact_mapping(module['changed_variables']))} |",
                f"| 数据与证据 | {_cell(_compact_mapping(module['data_scope']))} |",
                f"| 判定标准 | {_cell(_compact_mapping(module['thresholds']))} |",
                "",
                "#### 关键结果",
                "",
                *_metric_table(module["metrics"]),
                "",
                "#### 观察与分析",
                "",
                "**观察事实**",
                "",
                *_bullet_lines(module["facts"], empty="- 未产生新的实验事实。"),
                "",
                "**分析**",
                "",
                *_bullet_lines(module["interpretation"], empty="- 无法形成进一步解释。"),
                "",
                "#### 证据边界",
                "",
                f"- 选择影响：{_text(module['selection_impact'])}",
                *([f"- 跳过/失败原因：{_text(module['skip_reason'])}"] if module["skip_reason"] else []),
                *_bullet_lines(module["limitations"], empty="- 未记录额外限制。"),
                "",
                "#### 下一步/待确认",
                "",
                *_bullet_lines(module["user_input_needed"], empty="- 无需用户补充。"),
            ]
        )

    lines.extend(
        [
            "",
            "## 五、综合判断",
            "",
            f"{overall['executive_conclusion']}",
            "",
            ">完整代码路径、运行命令、产物清单、OOS 账本和选择记录保留在结构化审计底稿中，不在默认读者报告中展开。",
        ]
    )
    if include_technical_appendix:
        lines.extend(_technical_appendix(manifest))
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="structured audit manifest JSON")
    parser.add_argument("--output", required=True, help="Markdown report output path")
    parser.add_argument("--force", action="store_true", help="replace an existing report explicitly")
    parser.add_argument(
        "--include-technical-appendix",
        action="store_true",
        help="append code paths, commands, artifacts, ledgers and raw technical evidence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output_path = Path(args.output)
        if output_path.exists() and not args.force:
            raise ManifestError(f"output already exists: {output_path}; pass --force to replace it")
        manifest = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ManifestError("manifest root must be an object")
        report = render_report(manifest, include_technical_appendix=args.include_technical_appendix)
        output_path.write_text(report, encoding="utf-8")
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
