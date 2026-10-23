# 研究契约

## 目录

1. 冻结配置
2. 路线谱系
3. 预测、成员收益和信号
4. 正式账户结果
5. 产物和失败规则

## 1. 冻结配置

`study_config.json` 使用 UTF-8 JSON，至少包含：

```json
{
  "study_id": "example-route-study",
  "user_confirmed": true,
  "upstream": {
    "factor_evidence_hash": "...",
    "label_spec_hash": "...",
    "preprocessing_hash": "...",
    "model_spec_hash": "...",
    "account_spec_hash": "...",
    "data_hash": "...",
    "code_hash": "..."
  },
  "deployment_periods": [
    {
      "id": "2023",
      "information_cutoff": "2022-12-31",
      "selection_start": "2020-01-01",
      "selection_end": "2022-12-31",
      "deployment_start": "2023-01-01",
      "deployment_end": "2023-12-31",
      "evidence_role": "diagnostic_consumed"
    }
  ],
  "structural_search": {
    "profile": "custom",
    "axes": {
      "fdr_q": {"start": "0.05", "end": "0.50", "step": "0.005"},
      "distance_threshold": {"values": ["0.30", "0.50", "0.80"]},
      "linkage": {"values": ["average"]}
    }
  },
  "compression": {"target_medoids": 128, "nested_sizes": [32, 64, 128]},
  "model": {"seeds": [11, 29, 47]},
  "fixed_ensembles": {
    "route_equal_weight": 0.5,
    "factor_set_equal_weight": 0.5,
    "diagnostics": ["candidate_mass", "median", "trimmed_mean", "consensus_penalty"],
    "trim_fraction": 0.1,
    "consensus_penalty": 0.25
  },
  "selection": {
    "lookback_complete_calendar_years": 3,
    "metrics": [
      {"name": "sharpe", "direction": "max"},
      {"name": "calmar", "direction": "max"},
      {"name": "positive_month_rate", "direction": "max"}
    ],
    "n_values": [1, 3, 5, 10, 20, 100],
    "matches": 100,
    "seed": 2026,
    "overlap_threshold": 0.6,
    "top_k": [3]
  },
  "statistics": {"annualization": 252, "hac_lag": 5, "fdr_alpha": 0.1}
}
```

约束：

- `user_confirmed` 必须显式为布尔值。为 `false` 时只生成建议、可行性和工作量产物。
- 所有上游哈希必须是非空字符串，并纳入研究哈希。
- `deployment_periods` 按部署时间升序、不重叠；选择窗结束严格早于部署开始。
- `information_cutoff <= selection_end < deployment_start <= deployment_end`。
- `evidence_role` 只能是 `development_consumed`、`diagnostic_consumed` 或 `independent_forward`。
- 结构搜索轴只接受 `fdr_q`、`eligibility_status`、`dedup_method`、`correlation_aggregation`、`linkage`、`correlation_threshold`、`distance_threshold`、`representative_rule`。
- 每个轴使用非空 `values`，或者同时提供十进制 `start/end/step`。起点和终点都包含。
- 网格中的数值以规范化十进制字符串冻结，不使用二进制浮点累加。
- `target_medoids` 不得超过候选数；嵌套数严格递增且不超过 target。
- 固定融合的两个权重非负且和为 1。
- N 不得超过 medoid 数，脚本不会静默删除不可行 N。

`ema20_reference` 只会填充建议值；必须将 `profile` 改为 `confirmed_ema20_reference` 并显式确认后才可冻结。

## 2. 路线谱系

`route_factor_lineage.csv` 必需字段：

| 字段 | 含义 |
|---|---|
| `candidate_id` | 计划中的结构路线 ID |
| `deployment_id` | 冻结部署期 ID |
| `information_cutoff` | 本期路线可用信息截止 |
| `factor_id` | 稳定因子 ID |
| `factor_order` | 正整数，从1起且连续 |
| `factor_set_hash` | 规范化有序因子 ID 列表的 SHA-256 |
| `route_status` | 必须为 `completed` 才可用 |
| `source_evidence_hash` | 必须等于冻结上游因子证据哈希 |
| `route_spec_hash` | 必须与候选路线规格哈希一致 |

每个 `candidate_id + deployment_id + factor_order` 唯一。缺失部署期、空因子集、不连续顺序、重复因子或哈希不符会将路线标记为不完整。不完整路线留在失败分母，不进入距离矩阵。

## 3. 预测、成员收益和信号

`member_predictions.csv` 或 Parquet：

```text
deployment_id, decision_time, instrument_id, candidate_id, score, prediction_hash
```

每个 `deployment_id + decision_time + instrument_id + candidate_id` 唯一，`score` 有限。同一决策截面中每个正式成员必须覆盖相同的证券集合。

`member_returns.csv`：

```text
date, candidate_id, net_return
```

收益必须来自统一正式执行口径。同一 `date + candidate_id` 唯一，数值有限。

`member_signals.csv`：

```text
rebalance_date, candidate_id, asset_id
```

同一路线在同一调仓日可有多行。同一选择窗内的所有正式成员必须具有相同调仓日历和相同当日选标数。

## 4. 正式账户结果

`fixed_account_results.csv` 和 `selected_account_results.csv` 使用相同格式：

```text
configuration_id,date,net_return,continuous_account,account_hash
```

`continuous_account` 必须全部为 `true`。同一配置的日期不得重复。这些结果必须由目标策略的正式账户引擎生成，不接受成员净值的加权平均。

可选附加日频字段 `turnover`、`fill_rate`、`cash_weight`。提供时必须为有限数值，汇总器会输出总体及证据期均值；未提供时相应报告列留空，不伪造账户指标。

## 5. 产物和失败规则

- 输出目录必须不存在或为空；禁止覆盖。
- 每阶段 manifest 包含上游 manifest 哈希、输入哈希和自身产物哈希。
- 未计划的候选、部署期、N、指标或配置使整个阶段失败。
- 计划内失败项保留在 `failure_records.csv` 和完整分母中。
- `SUCCESS.json` 只表示计划分母已结算；可包含明确失败的路线或账户。
