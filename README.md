# Alpha Forge

Alpha Forge 是一个面向量化研究的 Codex Skills 仓库。每个 Skill 封装一类可重复使用的研究方法、执行约束和稳定计算脚本。

当前已提供：

- [`ml-strategy-overfitting-audit`](ml-strategy-overfitting-audit/SKILL.md)：机器学习量化策略过拟合检验。

## 设计原则

- 以被审计的策略仓库为事实来源。
- 复用现有训练、验证、预测和回测入口。
- 不在 Skill 中重新开发策略、训练器、数据管线或回测框架。
- 不能安全复用现有实现的实验应明确跳过，不用简化模拟替代。
- 结论必须能追溯到数据、配置、代码版本和实验产物。
- 默认生成结论优先的“总—分”报告，技术底稿按需追加。

## 安装

### 让 Codex 安装

在 Codex 中输入：

```text
请从 https://github.com/xiejinglover/alpha-forge 安装 ml-strategy-overfitting-audit skill。
```

Codex 会将 Skill 安装到本机 Skills 目录，新版本从下一个任务开始可用。

### 手动安装

```bash
git clone https://github.com/xiejinglover/alpha-forge.git
mkdir -p ~/.codex/skills
cp -R alpha-forge/ml-strategy-overfitting-audit ~/.codex/skills/
```

如果本机已存在同名 Skill，先备份旧目录，再安装新版本。

## 使用示例

在 Codex 中明确调用 Skill：

```text
使用 $ml-strategy-overfitting-audit 审计当前策略仓库的机器学习过拟合风险。
复用仓库已有的训练、验证和回测代码，并生成结论优先的总—分结构报告。
```

审计时，Skill 会：

1. 读取策略仓库指令、配置、测试和现有运行产物。
2. 定位真实的训练、验证、预测和回测入口。
3. 先复现基线，再使用现有代码执行适用的对照实验。
4. 分别判定证据资格、过拟合风险和选择污染。
5. 生成总体结论、模块总览和逐模块分析。

如果仓库中存在多个策略、多个训练入口，或 locked final OOS 身份不明，Skill 会先询问用户，不自行猜测。

## 检验模块

| 模块 | 检验内容 | 角色 |
|---|---|---|
| ML-001 | Train、development OOF 与 locked final OOS 隔离 | 证据完整性门禁 |
| ML-002 | 多随机种子稳定性 | 随机性泛化检验 |
| ML-003 | 特征、标签、信号与交易执行因果契约 | 因果合法性门禁 |
| ML-004 | 特征集、冗余与缺失口径消融 | 特征规格稳健性 |
| ML-005 | 训练配方与模型容量稳定性 | 容量过拟合检验 |
| ML-007 | Walk-forward 窗口与重训节奏 | 时间泛化检验 |
| ML-008 | 股票池、板块与不可交易样本迁移 | 横截面泛化检验 |
| ML-009 | 候选筛选、人工加回与 final-N 变更 | 选择诱发过拟合审计 |
| ML-010 | 标签与持有期成对敏感性 | 标签规格稳健性 |
| ML-011 | 主动特征噪声、缺失与来源偏差压力 | 扰动稳健性 |

`ML-006` 已从当前方法中移除，其他模块保留原编号，因此编号不连续。

## 报告结构

默认报告面向研究决策，采用以下结构：

1. 总体结论、关键发现、证据边界和优先行动。
2. 审计对象与执行阶段概览。
3. 十个模块的结论总览。
4. 每个模块的检验内容、关键指标、观察、解释、证据边界和下一步。
5. 综合判断。

代码路径、Shell 命令、完整 case、原始 JSON、产物清单、OOS 访问账本和选择记录默认只保留在结构化底稿中。需要复现细节时，可生成技术附录：

```bash
python3 ml-strategy-overfitting-audit/scripts/build_audit_report.py \
  --input audit-manifest.json \
  --output audit-report.md \
  --include-technical-appendix
```

## 脚本

脚本均只依赖 Python 标准库。它们不替代策略训练或回测框架，只用于审计现有实验产物。

| 脚本 | 用途 |
|---|---|
| `audit_temporal_integrity.py` | 检查时间因果、fold 归属、purge/embargo 和 OOS 访问污染 |
| `compute_stability_metrics.py` | 计算 IC、RankIC、Top-K Jaccard、排序相关、预测漂移和信号翻转 |
| `audit_model_selection.py` | 审计 trial 分母、容量族、训练—验证 gap、人工覆盖和后选择 |
| `build_audit_report.py` | 验证审计结果完整性并生成总—分结构 Markdown 报告 |

查看任一脚本的输入字段和命令行选项：

```bash
python3 ml-strategy-overfitting-audit/scripts/<script>.py --help
```

## 目录结构

```text
alpha-forge/
├── AGENT.MD
├── README.md
└── ml-strategy-overfitting-audit/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    ├── references/
    │   └── audit-execution-and-report.md
    └── scripts/
        ├── audit_temporal_integrity.py
        ├── compute_stability_metrics.py
        ├── audit_model_selection.py
        ├── build_audit_report.py
        └── tests/
```

## 测试

```bash
python3 -m unittest discover \
  -s ml-strategy-overfitting-audit/scripts/tests \
  -v
```

## 声明

本仓库提供的是量化研究和审计工具，不构成投资建议。历史回测、样本外结果或稳健性检验都不代表未来收益。
