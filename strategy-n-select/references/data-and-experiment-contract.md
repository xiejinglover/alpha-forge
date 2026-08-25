# 数据与实验契约

## 目录

1. 时间配置
2. 候选评分
3. 候选收益
4. Top1 信号
5. 选择与投票产物
6. 时间隔离和异常处理

## 1. 时间配置

`study.json` 使用 ISO 日期：

```json
{
  "development": {"start": "2023-01-01", "end": "2025-12-31"},
  "holdout": {
    "start": "2026-01-01",
    "end": "2026-07-20",
    "consumed_for_selection": false
  }
}
```

两个区间必须完整、互斥且开发期早于留出集。`consumed_for_selection=true` 时仍可生成诊断结果，但不得把结果表述为独立留出确认。

## 2. 候选评分

长表 CSV 必须包含：

| 字段 | 说明 |
|---|---|
| `candidate_id` | 稳定且唯一的候选标识 |
| `metric` | 指标名称 |
| `score` | 开发期评分；必须可解析为数值 |

同一 `candidate_id + metric` 只能有一行。评分 CSV 只允许承载开发期汇总结果。指标方向通过 N 选优命令的 `--direction max|min` 冻结。

## 3. 候选收益

内置评分脚本的输入 CSV 必须包含：

| 字段 | 说明 |
|---|---|
| `candidate_id` | 候选标识 |
| `date` | ISO 日期 |
| `net_return` | 已按统一执行和成本口径计算的单期净收益 |

每个 `candidate_id + date` 只能有一行。脚本只读取开发期范围，拒绝其中夹带的非有限收益。

内置指标：

- `sharpe`：单期均值除以样本标准差，再乘 `sqrt(annualization)`；默认年化因子 252、单期无风险收益 0。
- `calmar`：按完整开发期复利计算年化收益，再除以最大回撤绝对值。
- `positive_month_rate`：先按自然月复利，再计算月收益严格大于 0 的月份占比。

标准差为零、最大回撤为零或观察不足时输出空评分，使该候选不能在对应指标中获胜。

## 4. Top1 信号

留出期信号 CSV 必须包含：

| 字段 | 说明 |
|---|---|
| `rebalance_date` | 调仓日，ISO 日期 |
| `candidate_id` | 候选策略标识 |
| `asset_id` | 当日唯一 Top1 股票或 ETF 标识 |

同一 `rebalance_date + candidate_id` 只能有一行。脚本只读取留出期内信号。赢家在某调仓日缺少信号时，记录到诊断产物，不为其补票。

## 5. 选择与投票产物

选择阶段输出：

- `groups.csv`：每场抽到的全部候选；
- `winners.csv`：每场状态和唯一赢家；
- `members.csv`：每个 N 的唯一赢家、胜出次数和席位权重；
- `selection_manifest.json`：时间边界、默认参数、实际参数、覆盖情况、输入与产物哈希。

投票阶段输出：

- `member_votes.csv`：每个赢家的 Top1 选择及投票权；
- `missing_signals.csv`：缺少 Top1 信号的赢家；
- `asset_votes.csv`：两种成员投票方式下的完整标的票数与排名；
- `target_weights.csv`：`all` 和各 Top-K 的调仓目标权重；
- `portfolio_manifest.json` 和 `summary.md`：冻结来源、输入哈希和配置汇总。

## 6. 时间隔离和异常处理

- 在选择阶段不得读取留出期信号或回测结果。
- 在投票前校验选择清单及关键产物哈希。
- 不覆盖非空输出目录。
- 候选数小于 N 时失败，不改变 N 的含义。
- 全组评分均无效时记录 `invalid_group`，不重抽、不回退。
- 标的票数并列时按 `asset_id` 字符串升序破局。
- Top-K 大于等于当日获票标的数时保留全部标的，但仍保留该配置标签。
- 所有资金权重按入选标的票数重新归一化，并检查每个调仓日、N、投票方式和选择配置的权重和为 1。
