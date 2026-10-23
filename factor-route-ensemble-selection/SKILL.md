---
name: factor-route-ensemble-selection
description: 为滚动因子模型构造结构参数路线库，用不读取收益的年度因子谱系距离压缩路线，建立固定预测装袋基准，并执行年度滚动锦标赛 N 选优、选标去相关和正式账户比较。用于因子证据和冻结模型配方之后的路线装袋研究；不用于搜索模型超参、改变标签或持有路径、按诊断期挑选冠军，也不替代训练器或回测引擎。
---

# 因子路线装袋与滚动选优

本 Skill 只搜索会改变滚动因子集合的结构参数，把路线差异与模型随机性、交易规则差异分开。

开始前必须完整读取 [研究契约](references/study-contract.md)。计算路线距离或 medoid 时读取 [结构压缩](references/structural-compression.md)；生成预测组合或 N 选优时读取 [装袋与滚动选择](references/ensemble-and-selection.md)。

## 1. 先诊断和确认

检查目标仓库的真实候选池、标签、因子后处理、训练、预测、选股和正式账户入口，并验证上游：

- `rolling-factor-evidence` 的筛选配方、部署期谱系和证据哈希；
- `adaptive-factor-model-training` 的标签、预处理、模型、Seed、预测及账户契约哈希。

只向用户询问无法从仓库确定且会改变研究的选项：结构搜索轴、medoid 数、嵌套路线数、固定融合权重、滚动评分窗口、指标、N 网格、锦标赛次数、随机种子、选标重合阈值、证据期角色、HAC lag 和 FDR。

未确认时只生成可行性和资源报告，不启动正式谱系、训练或账户研究。

## 2. 冻结路线计划

使用：

```bash
python3 scripts/build_factor_route_plan.py \
  --config study_config.json \
  --output-dir runs/factor-route-plan
```

搜索轴只能是因子证据门槛、资格状态、去冗余方法、相关聚合、linkage、距离/相关阈值或代表规则。标签、模型参数、Seed、Top-N、持有和成交规则必须以上游冻结哈希引用；出现在结构搜索轴时直接失败。

向用户展示候选数、部署期数、谱系任务数和预计训练分母。只有 `user_confirmed=true` 时生成 `frozen_spec.json`。EMA20 报告参数只是可选 `ema20_reference` 建议档案，不得静默套用。

## 3. 结构压缩和模型复用

目标仓库适配器按 `candidate_route × deployment_period` 生成年度有序因子谱系，不在此阶段读取预测或账户收益。运行：

```bash
python3 scripts/run_factor_route_study.py structure \
  --plan-dir runs/factor-route-plan \
  --lineage route_factor_lineage.csv \
  --output-dir runs/route-structure
```

对各部署期因子集合计算 Jaccard 距离并等权平均，执行精确 Average-linkage，每簇按平均簇内距离选 medoid。并列按 `candidate_id` 升序。可选嵌套 farthest-point 集合只用于覆盖和计算收敛，不允许按账户表现选 L。

严格按年度、有序因子集合、预处理、模型、Seed、数据和代码哈希生成唯一模型身份。训练由上游冻结流程或目标仓库执行；本 Skill 不建立替代训练器。

## 4. 固定装袋基准

读取 [装袋与滚动选择](references/ensemble-and-selection.md)后运行：

```bash
python3 scripts/run_factor_route_study.py aggregate \
  --plan-dir runs/factor-route-plan \
  --structure-dir runs/route-structure \
  --predictions member_predictions.parquet \
  --output-dir runs/fixed-ensembles
```

每个成员先在真实决策截面内将预测转为平均百分位秩，再生成：

- 路线等权；
- 年度唯一因子集等权；
- 两者的预注册固定比例融合。

组合发生在预测层，然后重新选股并运行一个共享资金账户。不得平均成员净值。正式基准要求完整成员分母，不在当日动态忽略缺失成员。

## 5. 年度滚动 N 选优

运行：

```bash
python3 scripts/run_factor_route_study.py select \
  --plan-dir runs/factor-route-plan \
  --structure-dir runs/route-structure \
  --member-returns member_returns.csv \
  --member-signals member_signals.csv \
  --output-dir runs/rolling-selection
```

每个部署期只读取其开始前冻结窗口内的正式成员净收益和选标记录。各评分指标独立运行，但共享相同的随机小组清单。

每个 `deployment_id × N` 累计锦标赛胜出次数，按 `win_count` 降序、当前指标和 `candidate_id` 稳定破局。去相关只使用选择窗内相同调仓日的平均选标重合率；与任一已保留成员严格大于冻结阈值时删除。

分别产生胜出槽位加权和唯一成员等权，再生成全标的或冻结 Top-K 票数组合。年度目标权重必须拼成一个连续账户，不在年度边界重置现金、持仓或订单。

## 6. 评价和边界

对所有预注册 N 选优账户与同区间三个固定基准计算逐日净收益差 HAC，并在 `baseline × evidence_period` 家族内做 BH/FDR。只能用预注册开发期生成 Pareto 描述。

```bash
python3 scripts/run_factor_route_study.py summarize \
  --plan-dir runs/factor-route-plan \
  --fixed-accounts fixed_account_results.csv \
  --selected-accounts selected_account_results.csv \
  --output-dir runs/factor-route-report
```

每个区间必须标记为 `development_consumed`、`diagnostic_consumed` 或 `independent_forward`。不自动选冠军，不根据诊断期改参，不自动修改生产配置。

## 边界

- 不搜索模型超参、标签、持有期、选股或成交规则。
- 不按历史收益选 q、去冗余阈值、medoid 数、融合比例或 N。
- 不改写 `strategy-n-select`，也不在运行时导入其脚本。
- 找不到正式训练或账户入口时，只交付已完成的结构/预测产物并标记验证不完整。
