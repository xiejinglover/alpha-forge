# 因子模型研究契约

## 目录

1. 输入
2. 时间不变式
3. 路线契约
4. 模型契约
5. 预测与集成
6. 账户验证
7. 产物和状态

## 1. 输入

候选事件表必需语义：

```text
decision_time
instrument_id
pool_member
information_time
label_value
label_exit_time
label_valid
```

排序任务还需 `query_id`；如果不等于 `decision_time`，必须说明映射。可选保存计划/实际入场与退出时间、无效原因和样本权重。

PIT 因子长表或宽表均可，但必须能验证：

```text
factor_information_time <= decision_information_cutoff
```

每个因子需有稳定 `factor_id`、类型、可获得时间和版本。

上游因子系谱每行至少包含：

```text
deployment_id
screening_recipe_id
information_cutoff
factor_id
quality_rank
direction
eligibility_status
source_spec_hash
```

输入因子列表是有序的，列顺序纳入 `factor_set_hash`。

## 2. 时间不变式

对任何外层验证期 `v`：

```text
factor_information_time <= factor_cutoff_v
label_exit_time <= training_information_cutoff_v
preprocessing_fit_time < validation_start_v
model_fit_time < validation_start_v
```

内层早停必须重建因子路线。不得先用完整外层训练窗的标签选因子，再宣称最后一段是独立早停验证。

Embargo 使用真实交易日历和标签路径。如果实际退出会顺延，成熟性以实际 `label_exit_time` 为准。

## 3. 路线契约

`routes.jsonl` 每行是一条预注册路线：

```json
{
  "route_id": "raw",
  "screening_recipe_id": "recipe-a",
  "eligibility_status": "economic_eligible",
  "method": "raw",
  "correlation": null,
  "anchor": true
}
```

聚类路线需额外声明 `correlation_aggregation`、`distance`、`minimum_joint_observations`、`linkage`、`distance_threshold`、`representative_rule` 和 `boundary_comparison`。

因子集、列顺序、相关矩阵、聚类成员和预处理参数全部保存系谱与哈希。

## 4. 模型契约

`confirmed_spec.json` 至少包含：

```text
task_type
label_transform
model.family
model.version
model.params
early_stopping
outer_folds
preprocessing
seeds
ensemble_sizes
model_adapter.command
account_adapter.command
```

排序标签必须是有序相关度，并声明并列方法、分档边界、空组和退化 query 处理。回归标签必须声明单位、截尾和是否含成本。分类标签必须声明类别语义和样本权重。

训练适配器每个 trial 必须回传：

```text
trial_id
status
actual_train_rows
actual_validation_rows
actual_groups
actual_features
actual_iterations
model_hash
prediction_hash
metrics
```

## 5. 预测与集成

标准预测键为 `fold_id + decision_time + instrument_id + route_id + seed`。预测集成默认流程：

1. 每个成员在同一决策截面内对 score 做平均百分位秩；
2. 按冻结成员权重平均秩；
3. 重新执行策略选股和下单；
4. 使用正式引擎产生新账户。

不同路线或模型原始 score 只有在已校准且尺度兼容时才可直接平均。

## 6. 账户验证

正式引擎至少输出 orders、fills、positions、daily_nav、daily_returns、costs、turnover、rejections 和 summary_metrics。

预测指标与账户指标分开存储。相对锚点路线比较使用同日账户收益配对差，不以两个年化数直接相减替代。

## 7. 产物和状态

计划目录：

```text
frozen_spec.json
input_manifest.json
route_plan.jsonl
training_plan.jsonl
decision_log.json
```

执行目录：

```text
trial_status.csv
model_registry.csv
factor_route_lineage.csv
inner_validation_results.csv
daily_prediction_metrics.csv
seed_stability.csv
ensemble_registry.csv
account_run_manifest.csv
account_metrics.csv
increment_diagnostics.csv
failure_records.csv
REPORT.md
SUCCESS.json
```

`SUCCESS.json` 只表示计划分母已全部结算；它可以包含明确记录的失败 trial。如果正式账户适配器未完成，总状态不得为 `complete_account_validated`。
