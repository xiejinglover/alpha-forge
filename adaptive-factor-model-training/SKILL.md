---
name: adaptive-factor-model-training
description: 根据用户实际量化策略、滚动因子筛选结果、训练数据量和算力约束，交互式设计并执行因子后处理、时序滚动训练、早停、多 Seed 稳定性、预测集成和正式账户验证。用于因子边际证据之后构建排序、回归或分类模型；不用于自动搜索历史最优参数、按回测挑 Seed、重写目标仓库交易引擎或路线组合。
---

# Adaptive Factor Model Training

把完整工作分成“诊断—确认—冻结—执行”四个阶段。不要从任何历史项目复制标签、TopN、持有期、训练窗口或模型参数；先读取当前策略和数据。

## 1. 先诊断，再问用户

1. 检查目标仓库的数据集构建、模型训练、预测、选股和正式回测入口。
2. 读取上游因子证据产物，确认每个部署期的信息截止点、有序因子集和哈希。
3. 运行 `scripts/inspect_factor_model_context.py`，生成样本量、决策期数、截面规模、因子数、缺失率、标签分布和资源预算诊断。
4. 自动发现能从仓库确定的事实。只就无法推断且会改变研究结论的项目询问用户：学习目标、主指标、因子处理路线、模型家族、计算预算和正式执行口径。
5. 向用户展示推荐参数、推荐理由、预计模型数和资源需求。未确认前不得开始正式训练。

诊断和交互细则见 [自适应建议政策](references/adaptive-policy.md)。

## 2. 冻结学习与时间契约

根据策略目标选择 `cross_sectional_ranking`、`return_regression` 或 `classification`。任务类型不由标签值数量单独决定；分档标签可能仍然是排序任务。

每个方案必须声明：

- 原始经济标签、学习目标转换和并列值处理；
- 真实决策时点、候选池、训练分组和标签成熟条件；
- 外层训练/验证窗口、内层早停窗口和 embargo；
- 因子预处理、缺失值、类别值和异常值处理；
- 模型家族、版本、全部参数、主指标和 Seed 清单；
- 策略选股、权重、持有、成交和账户适配器。

训练集只能使用 `label_exit_time <= information_cutoff` 的成熟标签。因子筛选、方向、相关、聚类、编码、标准化和模型都在折内拟合。

完整字段和时间不变式见 [研究契约](references/study-contract.md)。

## 3. 构建因子后处理路线

从相同上游合格集合出发，根据用户确认生成一条或多条冻结路线。默认建议保留：

- `raw`：不去冗余的可解释锚点；
- `hierarchical_average`：使用训练折候选池内秩相关距离聚类，每簇按冻结质量顺序选代表。

也可使用 Greedy、Single 或 Complete，但路线、相关聚合口径、阈值、边界比较和代表规则必须在训练前冻结。不自动生成阈值网格，不根据账户结果追加路线。

## 4. 生成并执行冻结计划

用户确认 `recommended_spec.json` 后，写入 `confirmed_spec.json` 并显式设置 `user_confirmed=true`，然后运行：

```bash
python3 scripts/build_factor_model_plan.py \
  --profile-dir runs/model-context \
  --confirmed-spec confirmed_spec.json \
  --route-manifest routes.jsonl \
  --output-dir runs/model-plan

python3 scripts/run_factor_model_training.py \
  --plan-dir runs/model-plan \
  --output-dir runs/model-study
```

运行器按 `fold × route × seed` 保留固定分母，通过目标仓库模型适配器训练并产生预测。适配器协议见 [适配器指南](references/adapter-contract.md)。

早停阶段的因子集和预处理只能用内层拟合集重建。确定训练轮数后，在完整外层训练窗重建路线并重训。

## 5. 参数和 Seed 纪律

- 参数建议依据实际样本量、决策期数、截面规模、因子数、标签分布和算力预算。
- 所有建议都是待确认起点，不是通用最优值。
- 本 Skill 不做参数搜索。用户明确要求搜索时，改用专门的滚动调参流程，再把冻结参数交回本 Skill 执行。
- Seed 是稳定性维度，不是候选超参数。预先冻结完整清单和嵌套集成规模，禁止按表现挑 Seed。
- 预测集成在每个真实决策截面内先转换为百分位秩，再按冻结权重合成。不得平均成员账户净值来伪装预测集成。

## 6. 必须经过正式账户引擎

用目标策略的正式适配器把单模型和集成预测重新变成订单、成交和账户净值。持仓数、权重、调仓、成本、停牌、涨跌停、部分成交和现金规则全部来自策略契约，不在 Skill 内设固定值。

若找不到可验证的正式引擎，可完成预测层诊断，但必须标记 `account_validation_incomplete`，不得声称完成策略验证。

## 7. 结论与产物

报告预测指标、正式账户指标、分期稳定性、Seed 离散度、因子路线系谱和失败分母。只对预注册锚点路线执行整簇删除、同步置乱或 OOF 残差增量诊断。

不自动选冠军。完成目录不得覆盖；失败 trial 保留在 `failure_records.csv` 和固定分母中；同一输入、代码、环境和 Seed 应产生相同计划与预测哈希。
