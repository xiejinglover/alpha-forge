---
name: strategy-n-select
description: 对大量候选量化策略执行可复现的 N 选优研究：先确认开发期与留出集，只用开发期评分进行重复随机小组锦标赛，按当前指标和开发期选标重合率强制去相关，再按胜出次数加权或唯一成员等权汇总各策略原生数量的调仓标的投票，生成全标的或 Top-K 票数加权组合。用于用户要求实施、复现或审计 N 选优、赢家去相关、投票组合及其留出回测时；不用于门槛过滤或重新开发回测框架。
---

# 策略 N 选优

严格按以下顺序工作：

```text
确认时间边界 → 形成开发期评分 → N 选优 → 汇总胜出次数
→ 按当前指标与开发期选标重合率去相关 → 冻结新策略池
→ 分别运行两种成员投票 → 调仓日汇总标的票数
→ 全选或 Top-K → 按票数归一化资金权重 → 留出回测
```

先完整读取 [数据与实验契约](references/data-and-experiment-contract.md)，再运行脚本或评价结果。

## 1. 先确认时间边界

在读取策略表现前，必须向用户确认：

1. 开发期开始和结束日期；
2. 留出集开始和结束日期；
3. 留出集是否已用于选择候选、指标、`N`、`M`、Top-K 或其他参数；
4. 若已消费，是否存在新的未使用留出集。

用户未明确回答时暂停。拒绝重叠区间。开发期只用于评分和选优；留出集只能在名单、投票规则和参数冻结后读取。已参与选择的区间只能标记为诊断或验证数据，不能继续声称是独立留出集。

## 2. 适配目标仓库

先读取目标仓库指令、配置、数据样例、信号入口和回测入口，定位：

- 稳定唯一的 `candidate_id`；
- 开发期评分或候选净收益；
- `rebalance_date, candidate_id, asset_id` 语义的多标的信号；
- 原仓库正式组合回测入口。

自动生成字段映射；多个映射均合理时询问用户。只编写隔离的格式适配器，不复制训练、策略信号或交易执行逻辑。将适配结果转换为参考契约规定的 CSV。

## 3. 形成开发期评分

优先复用仓库已有评分。若只有日净收益，运行：

```bash
python3 scripts/compute_candidate_scores.py \
  --returns candidate_returns.csv \
  --study-config study.json \
  --output candidate_scores.csv \
  --metrics sharpe,calmar,positive_month_rate
```

每个指标必须单独运行 N 选优，不临时混合打分。自定义指标由目标仓库先计算，再转换为标准评分 CSV。

## 4. 执行 N 选优并冻结名单

默认使用：

```text
N = [1, 3, 5, 10, 20, 100, 300, 1000]
M = 100
seed = 2026
```

用户可以覆盖。候选数小于某个 N 时，不静默缩小小组；要求用户覆盖 N。运行单个评分指标：

```bash
python3 scripts/run_n_select.py select \
  --scores candidate_scores.csv \
  --study-config study.json \
  --metric sharpe \
  --direction max \
  --output-dir runs/sharpe-selection
```

脚本按冻结的 N 顺序使用一个 `random.Random(seed)` 随机流。每场无放回抽取 N 个候选；场与场之间独立重新抽样。`N=1` 是随机基线。分数并列时按 `candidate_id` 升序破局；缺失和非有限分数不能获胜；全组无有效分数时记录异常场，不回退、不重抽。

保留全部唯一赢家及原胜出次数，不对成员做 Top-K。原始成员池必须经过下一阶段去相关，不能直接进入投票。

## 5. 强制执行选标重合去相关

只使用开发期多标的信号运行：

```bash
python3 scripts/run_n_select.py decorrelate \
  --selection-manifest runs/sharpe-selection/selection_manifest.json \
  --development-signals development_signals.csv \
  --threshold 0.6 \
  --output-dir runs/sharpe-decorrelated
```

对每个 N，将唯一赢家按当前指标从优到劣排序，指标并列时按 `candidate_id` 升序。对同一开发期调仓日，所有策略必须选出相同数量 K 的标的；两策略当日相似度为交集数量除以 K，跨所有调仓日取平均。

第一名直接保留；后续策略依次与所有已保留策略比较。任一平均相似度严格大于 0.6 时删除当前策略，否则保留。不得使用收益、净值或留出期信号计算该相似度。

去相关后的 `decorrelated_members.csv` 是后续唯一策略池。删除策略的胜出次数消失且不转移；保留策略继续携带原 `win_count`，并在新池内重新归一化席位权重。

## 6. 分别生成两类投票组合

冻结选择产物后，读取留出期调仓信号并运行：

```bash
python3 scripts/run_n_select.py vote \
  --decorrelation-manifest runs/sharpe-decorrelated/decorrelation_manifest.json \
  --signals holdout_signals.csv \
  --top-k 1,3,5 \
  --output-dir runs/sharpe-portfolios
```

脚本必须从成员信号开始独立计算：

- `slot_weighted`：成员投票权等于胜出次数；
- `unique_equal`：每个唯一赢家投一票。

保留每个策略自己的选标数量。策略在某调仓日输出几只标的，就为每只标的投一张完整成员票：若成员投票权为 5 且输出 A、B、C、D，则四只标的各得 5 票，不在成员内部拆分或归一化。

对每个调仓日及每种成员投票方式，分别生成：

- `all`：保留所有获票标的；
- `top_k=K`：按票数降序、`asset_id` 升序稳定破局后保留前 K 个。

资金权重固定为入选标的票数占比。不要再增加标的等权分支。只输出调仓日目标权重；非调仓日由原仓库执行层维持上一目标持仓。

## 7. 回测与报告

先验证选择清单哈希，再把目标权重交给原仓库正式回测入口。没有可安全复用的入口时，交付选择产物和目标权重，明确组合层验证缺失；禁止新建简化回测器或用成员收益平均代替正式组合。

完整报告全部配置：

```text
N × metric × member_vote_mode × asset_selection
```

不得查看留出结果后挑选“最佳”配置。若需要正式选型，另设验证区间，并保留未消费的最终留出集。

## 边界

首版支持每个成员按原策略规则在调仓日输出任意正数量的标的。开发期去相关要求同一调仓日所有参与策略的选标数量相同。不实现资格门槛、成员 Top-K、入选标的等权或替代回测框架。
