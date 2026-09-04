# 数据与实验契约

## 目录

1. 研究配置
2. 候选与基准收益
3. 质量诊断
4. 控制因子、风险簇与信号
5. 产物契约
6. 时间隔离与异常处理

## 1. 研究配置

`study.json` 必须显式提供所有参数：

```json
{
  "family_id": "example-family",
  "annualization": 252,
  "quality_top_k": 100,
  "n_values": [3, 5, 10],
  "hac_lags": 5,
  "minimum_positive_residual_periods": 4,
  "decision_rules": {
    "minimum_return_retention": 0.8,
    "minimum_sharpe_delta": 0.0,
    "maximum_drawdown_worsening": 0.02,
    "minimum_ordinary_beta_reduction": 0.1,
    "minimum_downside_beta_reduction": 0.0,
    "minimum_tail10_beta_reduction": 0.0,
    "minimum_residual_alpha": 0.0,
    "minimum_config_pass_rate": 0.75
  },
  "decorrelation": {
    "enabled": true,
    "n_values": [3, 5],
    "pool_size": 30,
    "max_combinations": 200000,
    "cluster_constraint": {"enabled": true, "minimum_clusters": {"3": 3, "5": 4}, "max_per_cluster": 2}
  },
  "deployments": [
    {
      "deployment_id": "2025",
      "estimation": {"start": "2022-01-01", "end": "2024-12-31"},
      "evaluation": {"start": "2025-01-01", "end": "2025-12-31"},
      "benchmark_id": "family-full",
      "comparison_benchmark_ids": ["other-family-full"],
      "evidence_role": "consumed_research",
      "consumed_for_selection": true,
      "evaluation_dataset_id": "example-family-2025",
      "evaluation_accessed_at": "2026-01-05T09:00:00"
    }
  ]
}
```

`comparison_benchmark_ids` 可选；它们只用于评估已冻结组合的跨家族暴露，不参与成员选择。
一次 run 只允许一个策略家族和一个主基准；跨家族基准只放在 `comparison_benchmark_ids`。这使各部署期能够在日期不重叠时安全拼接为 `ALL_ROLLING`整段结果。某方案/N未覆盖全部批次时不生成整段指标。

`evidence_role` 只允许 `development`、`independent_holdout`、`consumed_research` 或 `forward_monitoring`。`consumed_for_selection=true` 时不得使用 `independent_holdout`。同一 deployment 的估计期必须严格早于评估期；不同 deployment 可以构成滚动验证。
`evaluation_dataset_id` 和 `evaluation_accessed_at` 必填。声称 `independent_holdout` 或 `forward_monitoring` 时还必须提供 `selection_frozen_at` 和64位 `selection_spec_hash`，并且与先前账本一致。

## 2. 候选与基准收益

`candidate_returns.csv`：

| 字段 | 约束 |
|---|---|
| `candidate_id` | 稳定且非空 |
| `date` | ISO 日期 |
| `net_return` | 有限小数，表示已扣费正式账户当期收益 |

`benchmark_returns.csv`：

| 字段 | 约束 |
|---|---|
| `benchmark_id` | 家族基准标识 |
| `date` | ISO 日期 |
| `net_return` | 与候选相同频率和成本语义的有限收益 |

同一主键不得重复。风险回归只使用候选与基准共有日期；估计期资格门禁和部署期组合必须覆盖基准的全部日期，不得静默丢日。

## 3. 质量诊断

`candidate_diagnostics.csv` 每个 `deployment_id + candidate_id` 唯一：

| 字段 | 类型 | 含义 |
|---|---|---|
| `deployment_id` | string | 对应滚动批次 |
| `family_id` | string | 候选所属家族，必须与 `study.json` 一致 |
| `candidate_id` | string | 候选标识 |
| `eligible` | bool | 目标仓库的基础资格结果 |
| `quality_score` | float | 只使用估计期得到的预注册质量分数，越大越好 |
| `trading_activity_ok` | bool | 交易活跃度门禁 |
| `coverage_complete` | bool | 账户、信号和必要风险数据覆盖门禁 |
| `positive_residual_periods` | int | 预注册分段中残差绩效为正的区间数 |
| `residual_periods_total` | int | 预注册分段总数 |
| `estimation_start,estimation_end,as_of` | date | 诊断所用窗口与信息截止日；必须匹配该批次估计期 |
| `rule_id` | string | 资格和质量规则的冻结版本 |
| `source_artifact_path,source_artifact_sha256` | string | 生成诊断的源产物路径与64位SHA-256；脚本必须实际复算匹配 |

目标仓库负责计算与交易语义相关的资格和质量分数；Skill 不发明新门槛。脚本会额外复算正复合收益、非零波动、完整收益覆盖和正残差 Alpha。

## 4. 控制因子、风险簇与信号

`control_returns.csv` 为长表：`date,factor_id,return`。同日控制因子集合必须一致；对某 deployment 控制数据不完整时停止该批次，不用零补齐。

`candidate_clusters.csv` 必须包含 `deployment_id,candidate_id,cluster_id,estimation_start,estimation_end,as_of,rule_id,source_artifact_path,source_artifact_sha256`。窗口和 `as_of` 必须匹配该 deployment 估计期，源产物哈希必须可复算；簇编号不必具有跨期经济含义。

`candidate_signals.csv` 必须包含 `date,candidate_id,asset_id`。信号重合只是诊断，不能代替残差收益相关性。本脚本计算成员对的平均日 Jaccard，不生成股票投票或目标权重。

`prior_oos_access_ledger.csv` 必须包含 `dataset_id,access_order,event_time,purpose,used_for_selection,status,selection_spec_hash,previous_event_hash,event_hash`。每个 `event_hash` 是该行除自身外字段的规范 JSON SHA-256，`previous_event_hash` 必须指向同数据集前一事件。声称 `independent_holdout` 或 `forward_monitoring` 时，该数据集最新的先前状态必须为 `prepared_not_accessed` 或 `locked_unconsumed`，不得存在 `used_for_selection=true`。

`frozen_selection_spec.json` 必须包含 `family_id,frozen_at,datasets`；每个数据集冻结估计/评估边界和主基准。脚本实际计算该文件 SHA-256，并要求它同时匹配 `study.json` 与先前账本。如果 `study.json` 含 `decision_rules`，相同阈值也必须在选择规格中冻结。哈希链用于发现所提供产物的事后修改，不构成外部可信时间戳或对恶意重造整条证据链的密码学防护。

## 5. 产物契约

输出目录必须为空或不存在。主脚本输出：

- `frozen_spec.json`：生效配置、输入哈希、运行环境、研究哈希和包含 `report.md` 的全部产物哈希；
- `candidate_risk_metrics.csv`：全候选分母、排除原因、质量排名和估计期风险；
- `portfolio_members.csv`：每期、每方案、每 N 的冻结成员与等权；
- `portfolio_returns.csv`：估计期与部署期线性袖套日收益；
- `portfolio_metrics.csv`：收益、Sharpe、回撤、Alpha和多类 Beta；
- `pairwise_diagnostics.csv`：残差相关、原收益相关和可选信号 Jaccard；
- `infeasible.csv`：不足 N、组合过多、覆盖不全和约束无解；
- `oos_access_ledger.csv`：每个部署期的证据身份与是否已用于选择；
- `report.md`：结论优先的中文报告。

## 6. 时间隔离与异常处理

- 所有排名、Beta、残差、相关和簇约束只使用估计期。
- 部署期只评估已冻结成员；不得将部署期收益反馈给同期选择。
- 数据重复、非有限数值、日期错序、重叠窗口、基准缺失或控制因子不完整时尽早失败。
- 候选部署期收益不全时只将相关配置记为 `infeasible`，不使用存活日期交集粉饰结果。
- 整个 N 网格必须完整保留。查看历史结果后改 N、质量保护数、风险指标或簇约束，必须新增后选择事件并降级相关数据身份。
