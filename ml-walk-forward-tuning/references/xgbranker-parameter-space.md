# XGBRanker 参数空间

本参考只在目标模型为 `xgboost.XGBRanker` 时读取。它提供适合横截面 Top1 排序研究的起始模板，不是脱离数据规模和标签定义的通用最优参数。

官方依据：[Learning to Rank](https://xgboost.readthedocs.io/en/stable/tutorials/learning_to_rank.html) 和 [XGBoost Parameters](https://xgboost.readthedocs.io/en/stable/parameter.html)。运行前还要核对目标仓库锁定的 XGBoost 版本；排序参数在 XGBoost 2.0 前后有明显变化。

## 数据和分组前提

- 每个训练样本必须带有 `qid` 或等价 group 信息；横截面选股通常以同一预测时点作为一个 query group。
- 输入必须按 query group 排序，训练、验证和预测使用同一分组语义。
- 标签若为多级相关度，优先使用 `rank:ndcg`；二元标签才考虑 `rank:map`。`rank:pairwise` 只能作为明确登记的替代 objective。
- Top1 策略的验证指标至少包含 `ndcg@1`，但不能用它替代正式成交后收益、风险和稳定性指标。

## 默认固定参数

以下参数默认固定，不参与第一轮搜索：

| 参数 | 起始值 | 处理方式 |
|---|---:|---|
| `booster` | `gbtree` | 固定；首版不混入线性或 DART booster |
| `tree_method` | `hist` | 固定；硬件或版本要求不同则由用户确认替换 |
| `objective` | `rank:ndcg` | 固定；改变 objective 必须建立独立 model space |
| `eval_metric` | `ndcg@1`, `ndcg@5` | 固定并与 Top1/诊断目标对齐 |
| `n_estimators` | `1000` | 作为早停上限，不与实际最佳迭代数混为一谈 |
| `gamma` | `0` | 第一轮固定；只有容量诊断需要时才加入后续搜索 |
| `reg_alpha` | `0` | 第一轮固定；稀疏正则研究另开候选轴 |
| `lambdarank_pair_method` | `topk` | Top1 场景的起始选择 |
| `random_state` | trial seed | 由计划注入，只用于重复稳定性，不按结果挑 seed |

## 第一轮搜索参数

建议从小而完整的搜索空间开始：

| 参数 | 起始候选 | 作用 |
|---|---|---|
| `max_depth` | `[3, 5]` | 控制树深和交互复杂度 |
| `min_child_weight` | `[1, 5]` | 限制叶节点的最小 Hessian 权重，抑制过细分裂 |
| `learning_rate` | `[0.03, 0.05]` | 控制每轮更新幅度，与早停上限共同使用 |
| `reg_lambda` | `[1, 5]` | L2 叶权重正则 |
| `lambdarank_num_pair_per_sample` | `[2, 3, 5]` | `topk` 模式下控制关注的排序顶部范围；Top1 时取略大于 1 的候选 |

这一模板产生 `2 × 2 × 2 × 2 × 3 = 48` 个模型配方。它还要与训练窗口、因子集合、标签、持仓和成交规则做完整笛卡尔积，因此生成计划后必须先展示总 trial 数。

## 可选的第二轮轴

只有用户明确要求且计算预算允许时，才把以下参数加入新的、完整登记的研究：

- `gamma`：例如 `[0, 0.1, 0.5]`；
- `reg_alpha`：例如 `[0, 0.1, 1]`；
- `objective`：把 `rank:ndcg` 与 `rank:pairwise` 拆成独立 model spaces；
- `lambdarank_pair_method`：把 `topk` 与 `mean` 拆成独立 model spaces，并分别定义 `lambdarank_num_pair_per_sample`，不要把含义不同的 pair 数盲目交叉。

全量搜索纪律仍然适用：不能根据中途结果删轴、删值或提前终止表现差的候选。

## `study_config.json` 模板

```json
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
  "early_stopping": {
    "mode": "outer_validation",
    "rounds": 50
  }
}
```

`random_state` 不写入固定参数，由 `trial_plan.jsonl` 中的 seed 交给目标仓库适配器。若使用外层验证期早停，所得 `actual_iterations` 必须进入结果长表。
