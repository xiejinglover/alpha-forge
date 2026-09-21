# 实验与数据契约

## 目录

1. 配置结构
2. 时间与窗口语义
3. 候选和 trial 枚举
4. 结果长表
5. 汇总产物
6. 完整性与失败规则

## 1. 配置结构

`study_config.json` 使用 UTF-8 JSON。示例：

```json
{
  "study_id": "example-2017-2019",
  "data": {
    "available_start": "2012-01-01",
    "available_end": "2019-12-31",
    "frequency": "daily",
    "calendar": "XSHG",
    "timezone": "Asia/Shanghai",
    "label_horizon_observations": 2,
    "purge_observations": 2,
    "embargo_observations": 0
  },
  "development": {
    "validation_periods": [
      {"id": "2017", "start": "2017-01-01", "end": "2017-12-31"},
      {"id": "2018", "start": "2018-01-01", "end": "2018-12-31"},
      {"id": "2019", "start": "2019-01-01", "end": "2019-12-31"}
    ],
    "training_windows": [
      {"id": "1y", "length": 1, "unit": "calendar_years"},
      {"id": "2y", "length": 2, "unit": "calendar_years"},
      {"id": "3y", "length": 3, "unit": "calendar_years"},
      {"id": "4y", "length": 4, "unit": "calendar_years"},
      {"id": "5y", "length": 5, "unit": "calendar_years"}
    ]
  },
  "factor_sets": {
    "core": ["value", "quality", "momentum"],
    "core_without_momentum": ["value", "quality"]
  },
  "fixed": {
    "selection_rule": "top1",
    "execution_rule": "next_open"
  },
  "search": {
    "factor_set": ["core", "core_without_momentum"],
    "label": ["return_2d", "return_5d"],
    "holding_rule": ["hold_2d", "hold_5d"]
  },
  "model_spaces": [
    {
      "id": "xgbranker-top1",
      "family": "xgboost_ranker",
      "fixed_params": {
        "booster": "gbtree",
        "tree_method": "hist",
        "objective": "rank:ndcg",
        "eval_metric": ["ndcg@1", "ndcg@5"],
        "n_estimators": 1000,
        "gamma": 0,
        "reg_alpha": 0,
        "lambdarank_pair_method": "topk"
      },
      "search_params": {
        "max_depth": [3, 5],
        "min_child_weight": [1, 5],
        "learning_rate": [0.03, 0.05],
        "reg_lambda": [1, 5],
        "lambdarank_num_pair_per_sample": [2, 3, 5]
      },
      "early_stopping": {"mode": "outer_validation", "rounds": 50}
    }
  ],
  "seeds": [11, 29, 47],
  "metrics": [
    {"name": "rank_ic", "direction": "max"},
    {"name": "net_sharpe", "direction": "max"},
    {"name": "max_drawdown", "direction": "min"}
  ]
}
```

约束：

- `study_id`、period/window/model/metric ID 均为非空字符串且在各自范围唯一。
- 日期使用 ISO `YYYY-MM-DD`。
- 首版训练窗口只支持 `calendar_years`；窗口按验证开始日前一天向前取完整日历年。
- `purge_observations >= label_horizon_observations`，三个 observation 字段均为非负整数。
- `fixed` 与 `search` 的键不得重叠；`search` 中每个候选列表非空且值不得重复。
- `factor_set` 无论固定还是搜索，其值都必须引用 `factor_sets` 中显式定义的集合。因子集合不能为空，集合内因子名不得重复。
- 每个模型的 `fixed_params` 与 `search_params` 键不得重叠；搜索参数列表非空且值不得重复。
- `early_stopping.mode` 使用 `disabled`、`inner_validation` 或 `outer_validation`。非禁用模式必须提供正整数 `rounds`。
- seed 是非负且不重复的整数。指标方向只能是 `max` 或 `min`。
- JSON 数值必须有限；不接受 `NaN` 或无穷值。

除 `factor_set` 外，业务维度的值可以是字符串、数值、布尔值或 JSON 对象。对象必须完整描述目标仓库适配器需要的规则。

## 2. 时间与窗口语义

验证区间必须按时间升序、互不重叠并完整落在数据覆盖内。开发期结束日等于最后一个验证区间结束日；计划和结果不得包含更晚 validation。

对验证区间 `[validation_start, validation_end]` 和 `N` 年窗口：

```text
training_end   = validation_start - 1 calendar day
training_start = validation_start shifted back N calendar years
```

例如 2017 年验证、1 年窗口得到 2016-01-01 至 2016-12-31；5 年窗口得到 2012-01-01 至 2016-12-31。purge 表示从训练尾部排除的 observation 数，由目标仓库按真实交易日历执行；脚本同时要求 purge 至少覆盖标签时域。

若 `training_start < data.available_start`，整个研究配置失败。脚本不会缩短窗口或删除该 fold。

## 3. 候选和 trial 枚举

候选配置为：

```text
training_window
× 每个 fixed/search 业务维度
× 某一个 model_space 内部的模型参数配方
```

多个 `model_spaces` 的配方取并集，不在模型之间交叉参数。一个候选跨所有验证期和 seed 执行：

```text
candidate × validation_period × seed = trial
```

`candidate_id` 和 `trial_id` 由规范化 JSON 的 SHA-256 前缀生成；相同输入必须得到相同 ID。`trial_plan.jsonl` 每行包含完整候选配置、训练/验证日期、purge/embargo、seed、指标和早停规则。

## 4. 结果长表

CSV 必须包含：

| 字段 | 说明 |
|---|---|
| `trial_id` | 计划中的 trial ID |
| `candidate_id` | 与 trial 对应的候选 ID |
| `validation_id` | 与 trial 对应的验证区间 ID |
| `seed` | 与 trial 对应的 seed |
| `metric` | 已完成 trial 的预注册指标；失败行留空 |
| `value` | 已完成指标的有限数值；失败行留空 |
| `status` | `completed` 或 `failed` |
| `artifact_path` | 可为空；目标仓库原始产物路径 |
| `error` | 失败原因；完成行可为空 |
| `actual_iterations` | 启用早停且 trial 完成时必填的正整数；失败行或禁用早停时可为空 |

完成 trial 必须恰好为每个预注册指标提供一行，且同一 trial 的状态、产物路径和实际迭代数一致。失败 trial 恰好提供一行空 metric/空 value，并给出非空错误。没有出现的 trial 记为 missing，不得从分母删除。

任何未知 trial、候选、validation、seed、指标，任何重复 `trial_id + metric`，或与计划不一致的身份字段都会使汇总失败。

## 5. 汇总产物

汇总器输出：

- `all_results.csv`：预期 trial × metric 的完整矩阵，包括 `completed`、`failed` 和 `missing`；
- `validation_summary.csv`：候选在每个验证区间、每项指标上的跨 seed 汇总；
- `candidate_summary.csv`：先对每个验证期跨 seed 取均值，再计算跨验证期均值、中位数、最差期和样本标准差；
- `metric_rankings.csv`：仅完整候选的逐指标排名；
- `completeness.csv`：每个候选的预期、完成、失败和缺失 trial 数；
- `pareto_candidates.csv`：在所有指标方向上非支配且结果完整的候选；
- `report.md`：研究分母、完整性、逐指标结果和 Pareto 集，不给唯一推荐。

`max` 指标的最差期取最小值，`min` 指标的最差期取最大值。只有一个验证期时标准差留空。

## 6. 完整性与失败规则

- 输出目录必须不存在或为空；禁止覆盖已有研究产物。
- 计划配置的任一窗口在任一验证期不可行时，计划整体失败。
- trial 失败或缺失时，候选标记为不完整，不进入指标排名或 Pareto 集。
- Pareto 比较使用 `candidate_summary.csv` 的跨验证期均值，并遵循每项指标预注册方向。
- 不生成加权综合分，不自动选择冠军，不生成冻结配置。
