---
name: rolling-factor-evidence
description: 在任意量化策略仓库中，把真实候选池、PIT 因子和与调仓/持有路径一致的成熟标签映射为标准契约，并滚动计算因子健康、同周期 RankIC、分组收益、HAC/FDR、经济幅度、稳定性和暴露诊断。用于实施、复现或审计因子边际证据；不用于相关去冗余、模型条件增量、模型调参、路线组合或正式账户回测。
---

# 滚动因子证据

本 Skill 只回答：一个因子在策略真实候选池和部署时可知的信息内，是否具有可复算的边际预测证据。

开始前完整读取 [研究与数据契约](references/study-contract.md)。涉及统计口径时读取 [统计方法](references/statistical-method.md)；目标仓库不是标准表时读取 [适配指南](references/adapter-guide.md)。

## 1. 先对齐策略周期

定位目标仓库的真实信号时点、调仓日、入场、退出、持有期、顺延、费用和候选池实现。标签必须复现这条路径，不能用固定 `t+H` 收益替代存在成交顺延或不同退出规则的策略收益。

每个标签必须由 `label_metadata.csv` 声明：

- `decision_schedule` 和决策间隔；
- `entry_rule`、`exit_rule`、持有期和执行延迟；
- `cost_model`、是否已扣费和是否存在重叠持仓。

排序策略默认以同周期 RankIC 为主检验、同周期分组 spread 为经济幅度；单因子分组策略默认相反。多持有期是不同研究问题，必须使用不同的 `label_id × screening_recipe_id`。

## 2. 适配而不重写目标仓库

优先复用仓库已有候选池、标签和 PIT 因子出口。若接口不一致，只在隔离运行目录生成转换器，把数据映射为契约规定的四张表；不要复制或替换生产标签器、训练器或回测器。

映射后先运行计划构建器。它只使用候选池结构、因子覆盖、时间戳和元数据，不读取 `label_value` 来决定阈值或分组数：

```bash
python3 scripts/build_factor_plan.py \
  --config study_config.json \
  --observations observations.csv \
  --factors factor_values.csv \
  --factor-metadata factor_metadata.csv \
  --labels label_metadata.csv \
  --output-dir runs/factor-plan
```

向用户展示 `structural_feasibility.csv`、建议组数、阈值和全部计划行。只有配置中 `thresholds_confirmed=true` 时才生成可执行冻结计划；不得静默采用建议值。

## 3. 执行冻结计划

```bash
python3 scripts/run_factor_evidence.py \
  --plan-dir runs/factor-plan \
  --observations observations.csv \
  --factors factor_values.csv \
  --factor-metadata factor_metadata.csv \
  --labels label_metadata.csv \
  --exposures exposures.csv \
  --output-dir runs/factor-evidence
```

`--exposures` 可省略。执行器会验证冻结输入哈希、PIT、标签路径声明和输出目录，再依次计算健康、日度证据、HAC/BH、经济幅度、稳定性和暴露诊断。

## 4. 结论边界

分别输出：

- `economic_eligible`：通过数据、主统计、经济幅度和稳定性门槛；
- `model_candidate`：PIT、覆盖、有效决策期和池内变化合格，可交给后续 OOF 条件增量研究。

无序类别因子首版只进入健康与模型候选通道，除非未来版本增加预注册的类别经济对比。不要把 `model_candidate` 表述为已证明有效，也不要把 `economic_eligible` 表述为联合模型增量。

失败、缺失和不可检验因子保留在固定分母中。不得根据结果改变阈值、主指标、窗口、标签、方向或 FDR family；需要改变时建立新的 `study_id` 和计划。

## 边界

- 不做相关删除、聚类、白化或因子容量 Top-K。
- 不训练模型、不挑 seed、不做 N 选优或账户集成。
- 不以 RankIC 或 spread 代替正式组合回测。
- 不把不同调仓/持有路径的结果直接合并。
