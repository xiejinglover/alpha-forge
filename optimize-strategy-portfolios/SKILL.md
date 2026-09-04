---
name: optimize-strategy-portfolios
description: 对已有量化候选策略的正式扣费账户执行可复现的组合优化与风险审计：先检查正残差 Alpha、交易活跃度和质量门槛，再计算普通、控制后、下行和尾部 Beta，比较质量等权、低 Beta、稳健 Beta、残差去相关及风险簇约束方案，并用滚动估计、下一期冻结验证和已消费证据账本防止后选择。用于用户要求组合大量候选策略、降低共同风险暴露、比较不同 N 或验证低 Beta Alpha 是否可迁移时；不用于训练模型、生成新信号、股票投票或新建交易回测器。
---

# 优化策略组合

严格按以下顺序工作：

```text
定位正式扣费账户与家族基准 → 冻结滚动窗口和规则
→ 复现基准与候选收益 → 执行 Alpha 和基础可用性门禁
→ 固定质量保护层 → 计算多类 Beta 与尾部共振
→ 完整运行 N 敏感性 → 可选残差去相关与风险簇约束
→ 冻结成员在下一期验证 → 报告全部配置与证据身份
```

运行前完整读取 [数据与实验契约](references/data-and-experiment-contract.md)。解释回归、筛选、相关性和结论时读取 [组合方法](references/portfolio-methodology.md)。

## 1. 适配目标策略仓库

先读取仓库指令、配置、数据样例、候选生成入口、正式账户收益和现有回测入口。定位：

- 稳定唯一的 `candidate_id`；
- 已经扣除成本的候选日收益；
- 每个策略家族的主基准；
- 时点可用的控制因子、交易活跃度、信号和风险簇数据；
- 权威基线 run 与可复用的正式组合回测入口。

只编写隔离的字段适配器，不复制训练、信号或交易执行逻辑。多个映射均合理时先询问用户。

## 2. 冻结研究规格

在读取任何部署期表现前，冻结：

- 估计期、部署期、年化因子和交易日历；
- 家族主基准及可选跨家族基准；
- 质量排名方向、质量保护数量、`N` 列表和稳定破局规则；
- 控制因子、HAC lag、去相关候选池和簇约束；
- 每个部署期的证据角色与 `consumed_for_selection`。

默认示例可使用前三年估计、下一年冻结验证、质量 Top100 和 `N=[3,5,10]`，但必须写入 `study.json`，不得作为隐藏默认。已参与调参的历史区间标记为 `consumed_research`，不得重新包装为独立 OOS。

## 3. 先检查 Alpha 与质量

低 Beta 不能创造 Alpha。对每个滚动估计期，只保留同时满足以下条件的候选：

- 目标仓库的 `eligible=true`；
- 估计期复合收益为正且波动率非零；
- 对家族主基准和控制因子回归的残差 Alpha 为正；
- 交易活跃度与账户、信号、风格暴露覆盖通过目标仓库的预注册门槛；
- 残差分段稳定性达到 `study.json` 中冻结的要求。

然后在合格分母中按冻结的质量分数保留 Top-Q。质量分数并列时按 `candidate_id` 升序。不得因 Beta 很低而豁免质量门禁。

## 4. 测量暴露并构建组合

对质量池计算普通 Beta、控制后 Beta、下行 Beta、最差10%/5% Beta、共同亏损率、残差 Alpha 和 HAC 标准误。每个 N 至少完整运行：

- `QUALITY_EQ`：质量最高 N 套等权；
- `LOW_BETA_EQ`：质量池内普通 Beta 最低 N 套等权；
- `ROBUST_BETA_EQ`：按普通、控制后、下行和最差10% Beta 的最差截面分位升序取 N 套。

只在组合数未超过冻结上限时枚举 `DECORRELATED_RISK_CAPPED_EQ`：要求四类估计期组合 Beta 都不高于同 N `LOW_BETA_EQ`，再依次最小化残差相关中位数、最大残差相关、尾部 Beta、普通 Beta 和候选 ID。提供风险簇时可再运行 `CLUSTER_CAPPED_DECORRELATED_EQ`；簇只限制集中度，不强制全簇等权。

组合均为已发布正式扣费账户日收益的等权线性袖套。不得用这一结果代替股票投票账户或联合资金账户。

## 5. 冻结验证与报告

成员、权重、N、基准和约束只能使用估计期信息，部署期不重排、不换人。完整报告全部 `deployment × N × scheme`，包括失败和不可行配置，不得只报历史赢家。

运行：

```bash
python3 scripts/run_portfolio_optimization.py \
  --study study.json \
  --candidate-returns candidate_returns.csv \
  --benchmark-returns benchmark_returns.csv \
  --diagnostics candidate_diagnostics.csv \
  --controls control_returns.csv \
  --clusters candidate_clusters.csv \
  --signals candidate_signals.csv \
  --prior-oos-ledger prior_oos_access_ledger.csv \
  --selection-spec frozen_selection_spec.json \
  --output-dir runs/portfolio-audit
```

`--controls`、`--clusters` 和 `--signals` 可选。只有全部评估期都标记为已消费研究证据时才可省略 `--prior-oos-ledger` 和 `--selection-spec`；声称独立留出或前瞻监测时必须提供已冻结选择规格及其 SHA-256，并提供之前未访问的逐事件哈希链账本。控制因子未提供时，控制后指标与普通回归口径一致并显式记录。使用 `build_portfolio_report.py` 从结构化产物重建中文 Markdown 报告。
脚本需要 Python 3 和 NumPy；先在目标环境验证依赖，不得静默切换数值实现。

## 与其他 Skill 的边界

- 候选产生、重复随机小组锦标赛、选标重合强制去重和股票投票使用 `$strategy-n-select`。
- 模型训练、标签、特征、股票池、容量和模型后选择过拟合审计使用 `$ml-strategy-overfitting-audit`。
- 本 Skill 只处理已有候选账户的质量保护、风险暴露和线性袖套组合。

## 失败原则

- 基准、时间身份、成本口径或证据是否已消费不明时暂停，不自行猜测。
- 基线不能复现时不执行依赖该基线的优化。
- 合格候选少于 N、部署期覆盖不全或组合约束不可行时，记录 `infeasible`；不降低 N、不放宽约束。
- 没有新的未消费数据时，最高结论为 `research_candidate_pending_forward`，不直接修改生产组合。
