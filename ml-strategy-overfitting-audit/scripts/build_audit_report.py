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


def render_report(manifest: Mapping[str, Any]) -> str:
    """Render a validated audit manifest as deterministic Markdown."""

    errors = validate_manifest(manifest)
    if errors:
        raise ManifestError("\n".join(f"- {error}" for error in errors))

    report = manifest["report"]
    overall = manifest["overall"]
    repository = report["repository"]
    strategy = report["strategy"]
    environment = report["environment"]
    modules_by_id = {module["module_id"]: module for module in manifest["modules"]}
    status_counts = Counter(module["execution_status"] for module in manifest["modules"])

    lines = [
        f"# {report['title']}",
        "",
        f"生成时间：{_text(report['generated_at'])}",
        "",
        "## 执行摘要",
        "",
        f"- `eligibility_verdict`：**{overall['eligibility_verdict']}**",
        f"- `overfitting_verdict`：**{overall['overfitting_verdict']}**",
        f"- 模块执行统计：{_text(dict(sorted(status_counts.items())))}",
        f"- 综合结论：{_text(overall['executive_conclusion'])}",
        "",
        "### 关键发现",
        "",
        *_bullet_lines(overall["critical_findings"]),
        "",
        "### 证据限制",
        "",
        *_bullet_lines(overall["limitations"]),
        "",
        "## 审计对象与可复现性元数据",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 仓库路径 | {_cell(repository['path'])} |",
        f"| 远程仓库 | {_cell(repository['remote'])} |",
        f"| 提交 | {_cell(repository['commit'])} |",
        f"| 分支 | {_cell(repository['branch'])} |",
        f"| 工作区有改动 | {_cell(repository['dirty'])} |",
        f"| 策略 | {_cell(strategy)} |",
        f"| 环境 | {_cell(environment)} |",
        f"| 审计范围 | {_cell(report['scope'])} |",
        "",
        "## 仓库理解与执行映射",
        "",
        "| 能力 | 代码路径 | 符号 | 原始命令 | 配置 | 输入 | 输出 | 证据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in manifest["repository_execution_map"]:
        lines.append(
            "| " + " | ".join(
                _cell(item[key])
                for key in ("capability", "paths", "symbols", "command", "config_paths", "inputs", "outputs", "evidence")
            ) + " |"
        )

    baseline = manifest["baseline"]
    lines.extend(
        [
            "",
            "## 基线复现",
            "",
            f"- 状态：`{baseline['status']}`",
            f"- 命令：`{_text(baseline['command'])}`",
            f"- 代码：{_text(baseline['code_paths'])}",
            f"- 配置：{_text(baseline['config'])}",
            f"- 数据区间：{_text(baseline['data_window'])}",
            f"- 产物：{_text(baseline['artifacts'])}",
            f"- 指标：{_text(baseline['metrics'])}",
            f"- 历史结果对比：{_text(baseline['historical_comparison'])}",
            f"- 阶段结论：{_text(baseline['conclusion'])}",
            "",
            "## 逐步骤执行日志",
            "",
            "| 步骤 | 阶段 | 状态 | 目标 | 动作 | 命令 | 输入 | 输出 | 结果 | 阶段结论 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for step in manifest["phase_log"]:
        lines.append(
            "| " + " | ".join(
                _cell(step[key])
                for key in ("step_id", "phase", "status", "objective", "action", "command", "inputs", "outputs", "result", "conclusion")
            ) + " |"
        )

    lines.extend(
        [
            "",
            "## 模块覆盖总表",
            "",
            "| 模块 | 优先级 | 执行方式 | 执行状态 | Verdict | 结论摘要 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for module_id in EXPECTED_MODULES:
        module = modules_by_id[module_id]
        summary = module["interpretation"] or module["skip_reason"] or module["facts"]
        lines.append(
            f"| {module_id} { _cell(module['title']) } | {_cell(module['priority'])} | {_cell(module['execution_mode'])} | {_cell(module['execution_status'])} | {_cell(module['verdict'])} | {_cell(summary)} |"
        )

    for module_id in EXPECTED_MODULES:
        module = modules_by_id[module_id]
        lines.extend(
            [
                "",
                f"## {module_id} {module['title']}",
                "",
                f"- 检验角色：{_text(module['role'])}",
                f"- 优先级：`{module['priority']}`",
                f"- 执行方式：`{module['execution_mode']}`",
                f"- 执行状态：`{module['execution_status']}`",
                f"- 阻塞原因类型：`{_text(module['blocked_reason_type'])}`",
                f"- 模块 verdict：**{module['verdict']}**",
                f"- 检验问题：{_text(module['question'])}",
                f"- 预注册假设：{_text(module['hypothesis'])}",
                "",
                "### 仓库证据与复用实现",
                "",
                "#### 已检查证据",
                "",
                *_bullet_lines(module["repository_evidence"]),
                "",
                "#### 复用代码",
                "",
                *_bullet_lines(module["reused_code"]),
                "",
                "### 实验设计",
                "",
                f"- 改变变量：{_text(module['changed_variables'])}",
                f"- 控制变量：{_text(module['controlled_variables'])}",
                f"- Cases：{_text(module['cases'])}",
                f"- 数据范围：{_text(module['data_scope'])}",
                f"- 判定阈值：{_text(module['thresholds'])}",
                "",
                "### 执行命令",
                "",
                *_json_block(module["commands"]),
                "",
                "### 指标与产物",
                "",
                "#### 指标",
                "",
                *_json_block(module["metrics"]),
                "",
                "#### 产物",
                "",
                *_bullet_lines(module["artifacts"]),
                "",
                "### 事实、解释与结论",
                "",
                "#### 观察事实",
                "",
                *_bullet_lines(module["facts"]),
                "",
                "#### 解释",
                "",
                *_bullet_lines(module["interpretation"]),
                "",
                f"- 选择影响：{_text(module['selection_impact'])}",
                f"- 跳过/失败原因：{_text(module['skip_reason'])}",
                "",
                "#### 限制",
                "",
                *_bullet_lines(module["limitations"]),
                "",
                "#### 所需用户输入",
                "",
                *_bullet_lines(module["user_input_needed"]),
            ]
        )

    lines.extend(["", "## OOS 访问账本", "", *_json_block(manifest["oos_access_ledger"])])
    lines.extend(["", "## 选择记录", "", *_json_block(manifest["selection_record"])])
    lines.extend(
        [
            "",
            "## 综合结论与建议",
            "",
            f"{overall['executive_conclusion']}",
            "",
            "### 建议与待确认事项",
            "",
            *_bullet_lines(overall["recommendations"]),
            "",
            "## 附录",
            "",
            "### 命令日志",
            "",
            *_json_block(manifest["command_log"]),
            "",
            "### 文件变更",
            "",
            *_json_block(manifest["file_changes"]),
            "",
            "### 错误与重试",
            "",
            *_json_block(manifest["errors"]),
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="structured audit manifest JSON")
    parser.add_argument("--output", required=True, help="Markdown report output path")
    parser.add_argument("--force", action="store_true", help="replace an existing report explicitly")
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
        report = render_report(manifest)
        output_path.write_text(report, encoding="utf-8")
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
