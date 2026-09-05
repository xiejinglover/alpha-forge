# 数据与实验契约

## 研究配置

`study.json` 必须明确声明：

```json
{
  "family_id": "example-family",
  "annualization": 252,
  "quality_top_k": 100,
  "quality_method": "ema20_robust_specific_quality_v1",
  "n_values": [1, 3, 5, 10, 15, 20],
  "hac_lags": 5,
  "minimum_positive_residual_periods": 4,
  "deployments": [
    {
      "deployment_id": "2025",
      "estimation": {"start": "2022-01-01", "end": "2024-12-31"},
      "evaluation": {"start": "2025-01-01", "end": "2025-12-31"},
      "benchmark_id": "family-full",
      "evidence_role": "consumed_research",
      "consumed_for_selection": true,
      "evaluation_dataset_id": "example-family-2025",
      "evaluation_accessed_at": "2026-01-05T09:00:00"
    }
  ]
}
```

`quality_top_k` 必须为100，`quality_method` 必须为 `ema20_robust_specific_quality_v1`。不接受 `raw_sharpe`、`annual_return` 或没有来源的通用 `quality_score`。

EMA20 原始 N 敏感性复现使用 `[1,3,5,10,15,20]`。新家族可以在评估期不可见时预注册其他 N，但必须在报告中区分“EMA20原网格”与“新家族预注册网格”。

## 候选与基准收益

`candidate_returns.csv` 包含 `candidate_id,date,net_return`；`benchmark_returns.csv` 包含 `benchmark_id,date,net_return`。`net_return` 必须是与目标仓库正式执行口径一致的已扣费账户收益。

同一主键不得重复。估计期和部署期必须完整覆盖家族基准日历，不得通过交集丢日粉饰结果。

## 质量诊断

`candidate_diagnostics.csv` 每个 `deployment_id + candidate_id` 唯一，必须包含：

| 字段 | 含义 |
|---|---|
| `deployment_id,family_id,candidate_id` | 稳定标识 |
| `eligible` | 目标仓库基础资格结果 |
| `robust_quality_score` | 按 EMA20 公式仅用估计期计算，越大越好 |
| `trading_activity_ok` | 交易活动度门槛 |
| `coverage_complete` | 账户和必需风险数据覆盖 |
| `positive_residual_periods` | 残差 Sharpe 为正的分段数 |
| `residual_periods_total` | EMA20 三年口径应为6 |
| `estimation_start,estimation_end,as_of` | 估计窗口和信息日 |
| `rule_id` | 必须可追溯到 EMA20 质量公式实现 |
| `source_artifact_path,source_artifact_sha256` | 源产物及可复算哈希 |

脚本会另外复算正复合收益、非零波动、完整收益覆盖、普通/控制后/下行/尾部 Beta 和正控制后 Alpha。

## 可选控制因子

`control_returns.csv` 是 `date,factor_id,return` 长表。同日因子集合必须一致；某部署批次覆盖不完整时停止，不用0补齐。

风险簇和候选股票信号不是标准流程的必需输入。未明确启动独立扩展实验时，不应加载它们。

## 输出

输出目录必须为空或不存在，且至少产生：

- `frozen_spec.json`：生效规格、输入哈希和环境；
- `candidate_risk_metrics.csv`：全候选分母、排除原因、`robust_quality_score`、质量排名与风险指标；
- `portfolio_members.csv`：每期每 N 的冻结成员与 `1/N` 权重；
- `portfolio_returns.csv`：估计期与部署期线性袖套日收益；
- `portfolio_metrics.csv`：收益、Sharpe、回撤、Alpha 和多类 Beta；
- `infeasible.csv`、`oos_access_ledger.csv` 和结论优先的 `report.md`。

## 时间和证据身份

所有资格、质量、Beta 和成员选择只使用估计期。部署期只评估已冻结成员。历史期一旦用于修改 N、Top100 或任何门槛，就必须标记为 `consumed_research`，不得重新包装成独立样本外。
