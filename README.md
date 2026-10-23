# Alpha Forge

Alpha Forge 是一个面向量化研究的 Codex Skills 仓库。每个 Skill 封装一类可重复使用的研究方法、执行约束和稳定计算脚本。

当前已提供：

- [`cne6-cluster-portfolios`](cne6-cluster-portfolios/SKILL.md)：按个股 CNE6 风格暴露滚动分簇，配置簇内代表，生成 TopN 投票或完整预测池平均排名股票组合。

- [`ml-walk-forward-tuning`](ml-walk-forward-tuning/SKILL.md)：在开发期内对训练窗口、模型参数、显式因子集合、标签和交易规则做全量滚动调参，完整报告逐期稳定性与 Pareto 候选，不自动选冠军或读取最终样本外。
- [`ml-strategy-overfitting-audit`](ml-strategy-overfitting-audit/SKILL.md)：机器学习量化策略过拟合检验。
- [`rolling-factor-evidence`](rolling-factor-evidence/SKILL.md)：按真实候选池、PIT 因子和与调仓/持有路径一致的成熟标签，滚动检验同周期 RankIC、分组收益、HAC/FDR、经济幅度与稳定性。
- [`adaptive-factor-model-training`](adaptive-factor-model-training/SKILL.md)：根据策略、训练数据量和算力约束，冻结因子后处理、滚动训练、早停、多 Seed 集成及正式账户验证。
- [`factor-route-ensemble-selection`](factor-route-ensemble-selection/SKILL.md)：搜索改变因子谱系的结构参数，收益盲化压缩滚动路线，并执行固定预测装袋和年度滚动 N 选优。
- [`strategy-n-select`](strategy-n-select/SKILL.md)：重复随机小组 N 选优、开发期选标重合去相关和调仓日全标的/Top-K 投票组合生成。
- [`optimize-strategy-portfolios`](optimize-strategy-portfolios/SKILL.md)：复现 EMA20 同口径的“残差稳健质量 Top100 → 普通 Beta 最低 N 套 → 冻结等权线性袖套”。

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

运行 N 选优研究时可明确调用：

```text
使用 $strategy-n-select 对当前策略仓库执行 N 选优。
先确认开发期和留出集，再分别生成胜出次数加权与唯一策略等权的调仓日投票组合。
```

如果候选不是彼此独立的既有策略，而是由因子证据门槛和相关去冗余参数生成的滚动模型路线，应使用独立的路线装袋流程：

```text
使用 $factor-route-ensemble-selection 读取已冻结的因子证据和模型配方，
构造结构参数路线库，以年度因子集合距离做收益盲化压缩，
建立路线等权、年度唯一因子集等权和固定融合基准，再执行年度滚动 N 选优。
```

对已有正式扣费候选账户进行组合风险优化时：

```text
使用 $optimize-strategy-portfolios 严格复现 EMA20 低 Beta 流程：使用 Skill 内置且经清单校验的九列日频控制因子收益，先按残差稳健质量分取 Top100，再按家族 full 基准的普通 Beta 选 N，对正式扣费账户做滚动冻结等权线性袖套。不要使用原始 Sharpe 取 Top100，不要加入股票投票、稳健 Beta、去相关或风险簇分支。
```

按风格暴露构建股票组合时：

```text
使用 $cne6-cluster-portfolios 对当前同类型候选策略做 CNE6 六簇组合。
用过去三年数据，每簇按总风险距离选 3 个代表，簇等权；
对完整预测池计算加权平均名次，选择前 10 只股票等权，复用正式回测。
先核对个股暴露与因子收益来源，并冻结参数。
```

也可指定 `top_n_vote`、`input_top_n=5`、`output_top_n=10`，使用各代表前 5 只股票投票选出前 10 只。

核心因子建模链路是：`rolling-factor-evidence` 负责同周期因子边际证据，`adaptive-factor-model-training` 负责冻结模型与滚动训练，`factor-route-ensemble-selection` 负责结构路线装袋和年度滚动 N 选优。`strategy-n-select` 继续用于彼此独立的既有候选策略；`ml-walk-forward-tuning` 负责模型或业务参数的开发期滚动调参；其他组合与审计 Skill 保持各自边界。

滚动筛选因子边际证据时可明确调用：

```text
使用 $rolling-factor-evidence 检查当前策略的真实调仓周期、候选池、PIT 因子和成熟标签，先冻结阈值与研究计划，再运行同周期 RankIC、分组收益、HAC/FDR、经济幅度和稳定性检验。不要执行相关去冗余、模型增量或账户回测。
```

机器学习开发期滚动调参可明确调用：

```text
使用 $ml-walk-forward-tuning 检查当前策略仓库的数据和训练入口，确认固定项与搜索项，生成完整笛卡尔积计划，并汇总全部开发期滚动验证结果和 Pareto 候选。不要读取最终样本外，也不要自动选出冠军。
```

`ml-walk-forward-tuning` 负责事前实验设计与开发期调参；`ml-strategy-overfitting-audit` 负责事后过拟合、时间隔离和选择污染审计。二者都优先复用目标仓库的真实训练与回测入口。

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

脚本不替代策略训练或交易回测框架。ML 审计与独立策略 N 选优脚本只依赖 Python 标准库；策略组合优化脚本额外依赖 NumPy；因子路线装袋脚本依赖 NumPy、SciPy、pandas 和 PyArrow，版本见该 Skill 的 `scripts/requirements.txt`。

| 脚本 | 用途 |
|---|---|
| `audit_temporal_integrity.py` | 检查时间因果、fold 归属、purge/embargo 和 OOS 访问污染 |
| `compute_stability_metrics.py` | 计算 IC、RankIC、Top-K Jaccard、排序相关、预测漂移和信号翻转 |
| `audit_model_selection.py` | 审计 trial 分母、容量族、训练—验证 gap、人工覆盖和后选择 |
| `build_audit_report.py` | 验证审计结果完整性并生成总—分结构 Markdown 报告 |
| `run_portfolio_optimization.py` | 使用内置控制因子运行质量保护、多类 Beta、等权组合和滚动冻结评估 |
| `validate_bundled_factor_returns.py` | 校验内置因子收益的哈希、日期、字段与数值完整性 |
| `build_portfolio_report.py` | 验证冻结产物哈希并重建组合审计报告 |
| `build_factor_route_plan.py` | 展开结构参数网格，生成可行性报告并冻结路线研究计划 |
| `run_factor_route_study.py` | 执行收益盲化路线压缩、固定预测装袋、年度滚动 N 选优和 HAC/FDR 汇总 |

查看任一脚本的输入字段和命令行选项：

```bash
python3 ml-strategy-overfitting-audit/scripts/<script>.py --help
```

## 目录结构

```text
alpha-forge/
├── AGENT.MD
├── README.md
├── ml-walk-forward-tuning/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/study-contract.md
│   └── scripts/
├── rolling-factor-evidence/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
├── adaptive-factor-model-training/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
├── factor-route-ensemble-selection/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
├── cne6-cluster-portfolios/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   └── references/
├── optimize-strategy-portfolios/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
├── strategy-n-select/
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

python3 -m unittest discover \
  -s ml-walk-forward-tuning/scripts/tests \
  -v

python3 -m unittest discover \
  -s rolling-factor-evidence/scripts/tests \
  -v

python3 -m unittest discover \
  -s strategy-n-select/scripts/tests \
  -v

python3 -m unittest discover \
  -s factor-route-ensemble-selection/scripts/tests \
  -v

python3 -m unittest discover \
  -s optimize-strategy-portfolios/scripts/tests \
  -v
```

## 声明

本仓库提供的是量化研究和审计工具，不构成投资建议。历史回测、样本外结果或稳健性检验都不代表未来收益。
