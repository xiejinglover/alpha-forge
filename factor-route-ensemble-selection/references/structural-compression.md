# 收益盲化的结构压缩

## 目录

1. 路线距离
2. Average-linkage 和 medoid
3. 嵌套覆盖
4. 年度模型身份复用
5. 数值和完整性

## 1. 路线距离

对完整路线 A、B，在每个预注册部署期 `t` 使用无序因子名集合：

\[
d_t(A,B)=1-\frac{|F_{A,t}\cap F_{B,t}|}{|F_{A,t}\cup F_{B,t}|}
\]

路线总距离是全部部署期的等权平均。所有部署期必须存在且因子集非空；不进行年度补值或动态重加权。

距离只使用因子 ID 集合。因子顺序仅用于训练身份哈希，不改变 Jaccard 距离。

## 2. Average-linkage 和 medoid

使用 SciPy `linkage(..., method="average")` 处理规范化 condensed 距离矩阵，再精确切成 `target_medoids` 个簇。候选按 `candidate_id` 排序后构建矩阵，保证相同输入的并列处理一致。

每簇的 medoid 定义为到簇内其他成员平均距离最小的候选。单成员簇直接选该成员。平均距离在数值容差内并列时，选 `candidate_id` 字符串最小者。

`candidate_to_medoid.csv` 至少保存候选、簇、medoid、到 medoid 距离和该簇代表的原候选数。

## 3. 嵌套覆盖

如果预注册 `nested_sizes`：

1. 从字符串最小的 medoid 开始。
2. 每次选择与当前集合最近距离最大的 medoid。
3. 并列按 `candidate_id` 升序。
4. 各 L 必须是前一个 L 的超集。

输出原候选到当前嵌套集合的最近结构距离均值、P95 和最大值。该结果不包含账户表现。

## 4. 年度模型身份复用

每个 medoid 的年度训练任务使用以下规范化对象哈希：

```json
{
  "deployment_id": "...",
  "ordered_factor_set_hash": "...",
  "preprocessing_hash": "...",
  "model_spec_hash": "...",
  "seed": 11,
  "data_hash": "...",
  "code_hash": "..."
}
```

唯一身份生成一个 `model_trial_id`。多条 medoid 路线命中同一身份时，`model_reuse_map.csv` 把它们指向相同 trial，不重复训练。

## 5. 数值和完整性

- 距离矩阵对称、对角为0，数值在 `[0,1]`。
- `route_distance.npz` 同时保存候选 ID 顺序，不允许脱离 ID 单独重用矩阵。
- 结构命令不接受 returns、NAV、Sharpe 或预测指标参数。
- NumPy 或 SciPy 不可用时在建立输出目录前失败，不替换为近似算法。
- 对已完成目录的重跑直接拒绝，不增量改写旧聚类。
