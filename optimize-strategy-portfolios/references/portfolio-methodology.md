# 策略组合方法

## 目录

1. 基本回归
2. 下行与尾部暴露
3. 资格与质量保护
4. 固定等权方案
5. 残差去相关
6. 结论语义

## 1. 基本回归

对候选收益 \(r_{i,t}\) 与家族基准 \(b_t\)：

\[
r_{i,t}=\alpha_i+\beta_i b_t+\epsilon_{i,t}.
\]

`ordinary_beta` 是上式的 \(\beta_i\)。提供控制因子 \(f_t\) 时：

\[
r_{i,t}=\alpha_i+\beta_i^{ctrl}b_t+\gamma_i^\top f_t+\epsilon_{i,t}.
\]

`controlled_beta` 是基准列的系数，`residual_alpha` 是截距的年化值。Alpha 的标准误使用 Newey–West HAC，lag 必须预先冻结。控制因子未提供时，`controlled_beta` 与 `ordinary_beta` 使用同一回归，但产物标记 `controls_supplied=false`。

线性袖套的组合日收益为：

\[
r_{p,t}=\sum_i w_i r_{i,t},\qquad \sum_i w_i=1.
\]

在相同日历和回归规格下，组合 Beta 应等于成员 Beta 的权重和；将此作为必须的数值校验。

## 2. 下行与尾部暴露

- `downside_beta`：只在 \(b_t<0\) 的日期估计带截距一元回归斜率。
- `tail_10_beta`：只在基准收益位于样本最差10%的日期估计。
- `tail_5_beta`：同理使用最差5%，样本少时只作诊断。
- `common_loss_rate`：基准亏损日中候选或组合同时亏损的比例。

条件样本少于三个或基准方差为零时，指标为空；不用零代替。

## 3. 资格与质量保护

低 Beta 方法的适用顺序是：

1. 使用目标仓库已冻结的 `eligible`、交易活跃度、数据覆盖和分段稳定性门禁；
2. 独立复算估计期正复合收益、非零波动和正残差 Alpha；
3. 再按 `quality_score` 从高到低保留 Top-Q；
4. 只在这个质量池内排序 Beta。

这些门禁只证明候选有资格进入风险优化，不证明 Alpha 能够前瞻延续。如果一个策略家族的冻结部署期控制后 Alpha 长期为负，应结论为“低 Beta 选择无法弥补 Alpha 不足”，而不是继续放宽风险约束。

## 4. 固定等权方案

`QUALITY_EQ` 是收益导向对照。`LOW_BETA_EQ` 按普通 Beta 升序和 `candidate_id` 升序稳定破局。

`ROBUST_BETA_EQ` 对普通、控制后、下行和尾部10% Beta 分别计算越低越好的截面百分位，取四项中最差百分位作为风险分数。分数升序，仍并列时按 `candidate_id`。

每个 N 都是独立冻结配置。历史上收益最高的 N 不自动成为生产 N。

## 5. 残差去相关

将质量池内候选日收益对家族基准和控制因子回归，使用估计期残差计算成员两两相关。

枚举方案必须先满足四类组合 Beta 不高于同 N `LOW_BETA_EQ`。在可行集中按以下字典序确定唯一组合：

1. 两两残差相关中位数最低；
2. 最大两两残差相关最低；
3. 组合尾部10% Beta 最低；
4. 组合普通 Beta 最低；
5. 排序后的 `candidate_id` 元组。

簇约束只检查最小簇覆盖数和单簇成员上限。不在簇间优化权重，不把簇编号解释为固定经济风格。

信号标的 Jaccard、原收益相关和残差相关必须分开报告。信号重合低不意味着共同收益机制低。

## 6. 结论语义

报告至少回答：

- 质量池是否具有可迁移的正 Alpha 证据；
- 低 Beta 相对 `QUALITY_EQ` 降低了多少普通、下行和尾部暴露；
- 收益、Sharpe 和回撤付出了什么代价；
- 改善是否在多个 N 和多个滚动期同向；
- Beta 下降是来自独立 Alpha，还是来自弱交易、低波动或收益消失。

使用下列语义：

- `infeasible`：数据或约束无法形成冻结组合；
- `diagnostic_only`：只有已消费历史证据；
- `research_candidate_pending_forward`：多个冻结滚动期支持研究候选，但仍等待新前瞻数据；
- `forward_supported`：只在预注册的未消费前瞻数据完成后才能使用。

`forward_supported` 还要求所有当期证据均为 `forward_monitoring`，并且选择规格中预先冻结收益保留率、Sharpe差、回撤恶化、普通/下行/尾部Beta降幅、残差Alpha和配置通过率阈值。任一条件不足时保持 `research_candidate_pending_forward`。

不从历史结果自行发明通过阈值，不将统计描述包装成严格因果归因。
