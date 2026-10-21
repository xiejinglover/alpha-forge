# 研究与数据契约

## 1. 标准输入

所有 CSV 使用 UTF-8。时间字段使用带时区的 ISO-8601，或在配置中声明时区的无时区 ISO 时间；同一次研究不得混用。

### `observations.csv`

一行表示一个真实候选池观察及一个标签。必需列：

| 列 | 语义 |
|---|---|
| `observation_id` | 同一 `pool_id × decision_time × instrument_id` 的稳定唯一 ID |
| `pool_id` | 实际交易候选池 ID |
| `decision_time` | 因子被观察并用于决策的时点 |
| `instrument_id` | 标的稳定 ID |
| `label_id` | 引用标签元数据 |
| `label_value` | 按真实执行路径计算的收益；可为空但不可为 Inf |
| `label_exit_time` | 该标签实际退出完成、因此成熟的时点 |

候选池大小由全部观察行计算，不得由因子非空子样本反推。一个 observation 可以为多个标签重复出现，但 `observation_id × label_id` 必须唯一，且重复行的池、时间和标的必须一致。

### `factor_values.csv`

必需列：`observation_id, factor_id, factor_value, factor_known_at`。可选列 `value_age_observations`。`observation_id × factor_id` 唯一；`factor_known_at` 不得晚于对应 `decision_time`。缺失值保留为空，Inf 非法。

### `factor_metadata.csv`

必需列：`factor_id, factor_type`。类型只能是 `continuous`、`ordinal`、`binary` 或 `nominal`。

### `label_metadata.csv`

必需列：

```text
label_id
decision_schedule
decision_interval_observations
entry_rule
exit_rule
holding_horizon_observations
execution_delay_observations
cost_model
label_is_net_of_costs
overlapping_positions
```

三个 observation 数字段为非负整数，决策间隔和持有期必须为正。`overlapping_positions` 必须等于 `holding_horizon_observations > decision_interval_observations` 的含义；复杂分批持仓无法用该声明表达时，不得使用本引擎，除非先把实际路径折算成独立标签。

### `exposures.csv`（可选）

必需列：`observation_id, exposure_id, exposure_type, exposure_value, exposure_known_at`。类型为 `continuous` 或 `categorical`，时间同样满足 PIT。

## 2. 配置

`study_config.json` 顶层字段：

```json
{
  "study_id": "example",
  "timezone": "Asia/Shanghai",
  "thresholds_confirmed": true,
  "deployments": [
    {
      "id": "2024H1",
      "information_cutoff": "2023-12-31T23:59:59+08:00",
      "windows": {
        "long": "2021-01-01T00:00:00+08:00"
      }
    }
  ],
  "recipes": [
    {
      "id": "rankic-5d-long",
      "consensus_id": "rankic-5d",
      "label_id": "net_return_5d",
      "window_id": "long",
      "primary_metric": "rank_ic",
      "strategy_contract": {
        "decision_schedule": "every_trading_day",
        "decision_interval_observations": 1,
        "entry_rule": "next_open",
        "exit_rule": "fifth_close",
        "holding_horizon_observations": 5,
        "execution_delay_observations": 1,
        "cost_model": "production_v1",
        "require_net_of_costs": true,
        "overlapping_positions": true
      },
      "preferred_group_count": 5,
      "fallback_group_count": 3,
      "minimum_pool_size": 30,
      "minimum_group_size": 10,
      "require_all_groups": true,
      "factor_non_null_ratio": 0.8,
      "valid_decision_coverage": 0.7,
      "minimum_valid_decisions": 30,
      "valid_decision_cap": 126,
      "fdr_alpha": 0.05,
      "confidence_level": 0.95,
      "hac_primary_lag": "auto",
      "hac_sensitivity_lags": [8],
      "minimum_effect": 0.001,
      "positive_period_share": 0.6,
      "monotonicity_threshold": 0.6,
      "minimum_consensus_windows": 1,
      "require_consistent_direction": true
    }
  ]
}
```

数值范围由构建器校验。`minimum_effect` 使用标签本身的收益单位，不能缺失。标签未扣费时，它至少覆盖完整成本和安全边际。

`hac_primary_lag="auto"` 时：

```text
ceil(holding_horizon_observations / decision_interval_observations) - 1
```

显式滞后不得小于这个重叠下界。敏感性滞后必须至少包含一个更长值；主结论始终使用主滞后。

## 3. 冻结与产物

计划目录包含 `frozen_spec.json`、`input_manifest.json`、`factor_plan.jsonl` 和 `structural_feasibility.csv`。计划行身份是 deployment、recipe、label、window 和 factor 的规范化 JSON 哈希。

执行目录必须不存在或为空，成功后包含：

```text
factor_health.csv
daily_factor_metrics.csv
factor_summary.csv
period_stability.csv
exposure_diagnostics.csv
period_factor_lineage.csv
failure_records.csv
REPORT.md
SUCCESS.json
```

任何计划输入哈希变化、未知因子/标签、PIT 违约、完成目录覆盖或非有限数值都会使运行失败。不可检验因子进入失败记录和固定分母，不因失败而从计划删除。
