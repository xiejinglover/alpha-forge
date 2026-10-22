# 目标仓库适配器契约

## 模型适配器

优先复用目标仓库已有数据集、训练和预测入口。若需新增薄适配器，使用无 shell 的 argv 命令模板：

```json
{
  "command": [
    "python3", "tools/run_model_trial.py",
    "--trial", "{trial_json}",
    "--output-dir", "{trial_output_dir}"
  ]
}
```

可用占位符只有 `trial_json`、`trial_output_dir`、`plan_dir` 和 `study_output_dir`。

每个 trial 目录必须产生：

```text
predictions.csv 或 predictions.parquet
metrics.json
model_manifest.json
SUCCESS.json
```

`predictions` 至少包含 `decision_time`、`instrument_id`和 `score`。`SUCCESS.json` 包含上述文件哈希、训练行数、验证行数、实际迭代数、依赖版本和线程设置。

适配器不得重新定义时间折、忽略冻结因子顺序或 Seed、从外层验证结果重选特征/轮数，也不得静默删除失败样本或变更 query group。

## 集成适配器

数据大或使用 Parquet 时，建议由目标仓库提供流式集成入口，读取冻结成员清单，输出每日每证券集成分数。必须对成员缺失制定固定规则：默认要求完整成员分母，不在当日动态忽略失败成员。

## 账户适配器

账户命令模板：

```json
{
  "command": [
    "python3", "tools/run_formal_account.py",
    "--prediction-manifest", "{prediction_manifest}",
    "--strategy-spec", "{strategy_spec}",
    "--output-dir", "{account_output_dir}"
  ]
}
```

账户引擎必须是该策略正式研究或生产所用引擎。适配器只转换预测格式，不在旁边写一个简化回测替代它。

预期产物为 orders、fills、positions、daily_returns、daily_nav、metrics.json、engine_manifest.json 和 `SUCCESS.json`。

## 运行器行为

`run_factor_model_training.py` 负责固定分母和进程管理，不解释目标项目的业务字段。它对每个 trial：

1. 写入不可变 `trial.json`；
2. 调用命令数组，不使用 `shell=True`；
3. 保存 stdout/stderr 和返回码；
4. 校验必需产物；
5. 将成功或失败写入状态长表；
6. 已存在的完成目录拒绝覆盖。

若用户允许继续其他 trial，单个失败不可改变计划分母；若设为 fail-fast，未执行项仍保留 `not_run_after_failure` 状态。
