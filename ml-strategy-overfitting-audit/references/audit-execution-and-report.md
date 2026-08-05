# 审计执行与报告规范

## 目录

- [目标](#目标)
- [仓库发现](#仓库发现)
- [实验执行边界](#实验执行边界)
- [交互与跳过规则](#交互与跳过规则)
- [逐步骤日志](#逐步骤日志)
- [逐模块报告](#逐模块报告)
- [完整报告结构](#完整报告结构)
- [结构化输入](#结构化输入)
- [完成检查](#完成检查)

## 目标

以现有策略仓库为唯一策略实现来源，复用其训练、验证、预测和回测能力完成过拟合审计。让读者仅凭报告即可回答：

1. 审计了哪个代码版本、数据区间和策略口径？
2. 总体是否存在过拟合风险，最关键的依据是什么？
3. 审计的各阶段做了什么，得到了什么结论？
4. 每个模块检验了什么，观察到什么，结论是什么？
5. 哪些模块未执行，为什么未执行，对总判断有什么影响？
6. 是否发生 OOS 污染、后选择或人工覆盖？

## 仓库发现

按顺序执行，只读取必要文件：

1. 确认仓库根目录、当前分支、提交哈希和工作区状态。
2. 读取适用的 `AGENTS.md`、`AGENT.MD`、README、依赖与环境文件。
3. 使用 `rg --files` 和 `rg` 定位训练、验证、预测、回测、数据加载、配置和指标实现。
4. 沿真实调用关系读取入口文件、配置解析、数据切分、标签生成、模型构建和结果写出代码。
5. 查找已有命令、任务编排、测试、notebook、历史日志、模型产物和报告。
6. 确定唯一审计对象：具体 ML 策略、生产配置或模型插件、基线 run、development 数据和 locked final OOS。
7. 建立 `repository_execution_map`，不要在尚未理解现有入口时设计实验。

如果仓库只是通用框架，或存在多个候选策略而无法从当前配置、运行记录或文档确定目标，列出候选项及差异并询问用户。未确定唯一对象前，不得选用某个历史示例充当当前策略。

每个执行能力至少记录：

| 字段 | 内容 |
|---|---|
| `capability` | `data`、`train`、`validate`、`predict`、`backtest` 或 `report` |
| `paths` | 实际代码路径 |
| `symbols` | 函数、类、命令或任务名称 |
| `command` | 仓库原始运行命令 |
| `config_paths` | 配置来源 |
| `inputs` | 数据、模型、配置输入 |
| `outputs` | 预测、指标、模型、账本等产物 |
| `evidence` | 支持该映射的代码行、测试、日志或历史产物 |

无法确认某个映射时写 `null` 并列入 `missing_facts`，不要根据常见框架猜测。

## 实验执行边界

### 必须复用

- 调用仓库已有 CLI、任务、函数或类。
- 使用仓库已有配置系统表达 seed、特征、容量、窗口、标签和股票池 case。
- 使用仓库已有数据加载、预处理、标签生成、fold、模型训练、预测和回测语义。
- 使用仓库已有环境、依赖锁定和计算入口。
- 先复现基线，再执行任何对照实验。

### 允许的最小改动

- 新增只包含实验参数覆盖的配置文件。
- 编写调用现有入口的批处理包装器。
- 编写产物收集、格式转换和审计指标汇总代码。
- 在不改变默认行为的前提下，为现有入口暴露必要参数。
- 修复阻止原有训练/验证运行的明确缺陷，但必须单独记录并先取得用户授权。

“最小适配”只能传递参数、调用已有入口和收集产物。如果适配器需要自行实现多 seed、fold/OOF、训练、预测、标签、噪声注入或回测语义，该实验没有安全复用路径，不得以适配之名重新开发。

### 禁止事项

- 重写训练循环或模型类来替代仓库实现。
- 新建另一套数据清洗、标签、fold、组合或回测框架。
- 将策略核心逻辑复制进 Skill 脚本后独立运行。
- 为了让实验可运行而改变数据口径、交易语义、成本、股票池或标签。
- 覆盖原始模型、预测、缓存、账本或历史报告。
- 仅凭静态代码推断把模块标记为 `PASSED`。
- 因现有代码难以复用而静默改用简化模拟。

历史日志、文档示例和其他策略产物只能用于定位仓库能力，不是当前审计对象的实验证据，除非已证明它们对应同一提交、配置、数据口径和策略。

所有实验使用隔离输出目录，例如仓库既有实验目录或用户认可的临时目录。记录原始命令和实际命令，不能只写自然语言摘要。

## 交互与跳过规则

先尝试从代码、配置、测试、日志和产物发现答案。仍存在下列歧义时询问用户：

- 存在多个训练或回测入口，无法确定生产真源。
- 参数名称相同但业务含义不同。
- 数据路径、密钥、许可或昂贵计算需要用户决定。
- 改动会改变策略语义、历史口径或外部系统状态。
- locked final OOS 的身份或访问权限不明确。

提问时给出已发现的候选路径、差异和推荐选择。一次只问阻塞当前进度的关键问题；等待回复时继续执行不依赖该答案的模块。

满足以下任一条件时允许跳过实验：

- 仓库没有相应训练/验证能力，完成实验必须新建替代框架。
- 必要数据、模型、环境、依赖、硬件或访问权不可用。
- 现有代码不能表达所需 case，最小参数暴露仍会改变策略语义。
- 模块根据策略类型确实不适用。

跳过时必须记录：

- `execution_mode`：`NO_SAFE_REUSE_PATH` 或 `NOT_APPLICABLE`。
- `execution_status`：`SKIPPED_UNAVAILABLE` 或 `NOT_APPLICABLE`。
- `blocked_reason_type`：`MISSING_USER_DECISION`、`EXECUTION_NOT_AUTHORIZED`、`MISSING_REPOSITORY_CAPABILITY`、`MISSING_DATA_OR_ENVIRONMENT` 或 `null`。
- 已检查的代码、配置、日志和产物。
- 具体阻断事实，不能只写“无法执行”。
- 尝试过的安全替代方案及其失败原因。
- 恢复实验需要的用户输入、数据或仓库能力。
- 对模块 verdict 和总 verdict 的影响。

`NEEDS_USER_INPUT` 表示缺少必要的用户决策或执行授权；`SKIPPED_UNAVAILABLE` 表示已确认仓库、数据或环境没有安全执行路径。两者不得混用。`SKIPPED_UNAVAILABLE` 不能记为通过；通常对应模块 `BLOCKED`。`NOT_APPLICABLE` 必须提供策略事实依据。

## 逐步骤日志

从仓库发现开始连续编号，不只记录成功命令。每一步包含：

```yaml
step_id: STEP-001
phase: repository_discovery
status: COMPLETED
objective: 定位生产训练入口
action: 检索 CLI、配置与训练调用关系
command: rg -n "train|fit|walk_forward" ...
inputs: [仓库代码]
outputs: [src/train.py, configs/model.yaml]
result: 发现 python -m project.train 是唯一被测试覆盖的入口
conclusion: 后续实验复用该命令
```

阶段至少覆盖：

1. `repository_discovery`
2. `baseline_reproduction`
3. `experiment_planning`
4. `experiment_execution`
5. `evidence_aggregation`
6. `final_assessment`

每个阶段都必须出现，`status` 为 `COMPLETED`、`PARTIAL`、`BLOCKED`、`SKIPPED` 或 `NOT_STARTED`。未运行 shell 命令的阅读和判断步骤也要记录 `action`、`outputs` 与 `conclusion`，`command` 可为 `null`。

## 逐模块报告

报告必须按 `ML-001`、`ML-002`、`ML-003`、`ML-004`、`ML-005`、`ML-007`、`ML-008`、`ML-009`、`ML-010`、`ML-011` 顺序出现。每个模块在正文中先给一句话结论，再按以下四段展开。

### 结论与状态

- 模块名称、检验角色和优先级。
- `execution_status`：`COMPLETED`、`PARTIAL`、`NEEDS_USER_INPUT`、`SKIPPED_UNAVAILABLE`、`NOT_APPLICABLE` 或 `FAILED_TO_RUN`。
- 模块 verdict：`PASSED`、`FAILED`、`BLOCKED`、`INCONCLUSIVE` 或 `NOT_APPLICABLE`。
- 一句话解释 verdict，不得只显示状态码。

### 检验了什么

- 要回答的问题与预注册假设。
- 用自然语言概括改变的变量、case 数、数据范围和样本量。
- 唯一改变的变量和保持不变的控制变量。
- 判定阈值及其来源；没有阈值时明确写 `INCONCLUSIVE` 风险。

### 关键结果

- 使用紧凑表格报告最能支持结论的指标，正文最多 12 条。
- 完整 case 和指标分母仍保留在结构化底稿，不只保留赢家。
- 区分 train、development OOF/internal walk-forward 和 locked final OOS。

### 观察、解释与边界

- 先列观察事实，再给解释，最后给 verdict。
- 说明证据支持什么、不支持什么。
- 说明对 `selection_record` 和 final OOS 资格的影响。
- 列出限制、缺失事实、跳过原因和下一步。

代码路径、符号、命令、配置哈希、产物路径、原始 JSON、`execution_mode`、`blocked_reason_type`、OOS 账本和 `selection_record` 属于技术底稿。默认正文不展开；用户明确要求复现细节、代码审查或技术附录时再输出。

## 完整报告结构

默认按以下“总—分”顺序生成 Markdown 报告：

1. 总体结论：verdict、最重要发现、证据边界、优先行动。
2. 审计对象与执行概览：策略、版本、数据范围，以及六个阶段“做了什么/结论是什么”。
3. 模块结论总览：十个模块的状态、verdict 和一句话摘要。
4. 各模块详细分析：结论置顶，再给检验内容、关键指标、观察解释、证据边界和下一步。
5. 综合判断。

使用 `--include-technical-appendix` 时才在末尾追加仓库映射、代码路径、命令、产物、完整 case、OOS 账本、选择记录、错误和文件变更。

总体结论必须至少回答：

- `eligibility_verdict`
- `overfitting_verdict`
- 完成、部分完成、失败、跳过和不适用模块数量
- 最高优先级结构性违规
- locked final OOS 是否仍可承担最终确认
- 当前结论最重要的证据限制

## 结构化输入

将实验结果保存为 JSON，再运行 `build_audit_report.py`。顶层结构如下：

如果当前任务是只读审计或不允许创建文件，不得为了使用报告生成器而擅自写入仓库；直接在最终回复中按同一完整结构交付报告。

```json
{
  "report": {
    "title": "ML strategy overfitting audit",
    "generated_at": "ISO-8601",
    "repository": {"path": "...", "remote": "...", "commit": "...", "branch": "...", "dirty": false},
    "strategy": {"name": "...", "model": "...", "market": "...", "frequency": "...", "label": "...", "decision_time": "...", "execution_time": "..."},
    "environment": {"python": "...", "dependencies": "...", "hardware": "..."},
    "scope": []
  },
  "overall": {
    "eligibility_verdict": "INCONCLUSIVE",
    "overfitting_verdict": "INCONCLUSIVE",
    "executive_conclusion": "...",
    "critical_findings": [],
    "limitations": [],
    "recommendations": []
  },
  "repository_execution_map": [],
  "baseline": {
    "status": "...",
    "command": "...",
    "code_paths": [],
    "config": "...",
    "data_window": "...",
    "artifacts": [],
    "metrics": {},
    "historical_comparison": "...",
    "conclusion": "..."
  },
  "phase_log": [],
  "modules": [],
  "oos_access_ledger": [],
  "selection_record": {},
  "command_log": [],
  "file_changes": [],
  "errors": []
}
```

每个 `modules` 元素必须包含：

```json
{
  "module_id": "ML-001",
  "title": "...",
  "role": "...",
  "priority": "P0",
  "execution_mode": "REUSE_DIRECT",
  "execution_status": "COMPLETED",
  "blocked_reason_type": null,
  "verdict": "INCONCLUSIVE",
  "question": "...",
  "hypothesis": "...",
  "repository_evidence": [],
  "reused_code": [],
  "commands": [],
  "changed_variables": {},
  "controlled_variables": {},
  "cases": [],
  "data_scope": {},
  "thresholds": {},
  "metrics": [],
  "artifacts": [],
  "facts": [],
  "interpretation": [],
  "limitations": [],
  "skip_reason": null,
  "user_input_needed": [],
  "selection_impact": "..."
}
```

发现阶段尚未确定的标量用 `null`，未执行产生的集合用 `[]`，例如 `commands`、`metrics` 和 `artifacts`。不得省略必需字段，也不得用虚构结果填充；`facts`、`interpretation`、`conclusion` 或 `skip_reason` 应说明为何尚未执行。

`commands` 元素记录 `command`、`status`、`exit_code`、`output_summary` 和 `artifacts`。`metrics` 元素至少记录 `case_id`、`evidence_role`、`metric`、`value`、`unit` 和 `sample_count`。

## 完成检查

交付前确认：

- 报告包含全部十个模块，顺序正确。
- 正文先给总体结论和模块总览，再进入逐模块分析。
- 默认正文没有代码路径、shell 命令、原始 JSON 或完整 case 堆叠。
- 每个执行模块都能追溯到真实仓库代码、命令和产物。
- 每个跳过模块都有具体原因、已检查证据和结论影响。
- 每一步都有动作、结果和阶段结论。
- 没有把 development OOF 写成 locked final OOS。
- 没有使用 final OOS 选择 seed、特征、配方、窗口、股票池或标签。
- 没有重新实现策略、训练或回测框架。
- 没有只报告赢家或省略失败 trial。
- 没有在缺少阈值、数据或证据时声称通过。
- 报告路径、代码提交、配置和产物足以复现结论。
