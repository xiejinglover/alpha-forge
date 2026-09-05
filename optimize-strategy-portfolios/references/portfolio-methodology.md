# 风险指标与组合口径

## 基准回归

对候选收益 (r_{i,t}) 与家族 full 基准 (b_t)：

\[
r_{i,t}=\alpha_i+\beta_i b_t+\epsilon_{i,t}.
\]

`ordinary_beta` 是主选择指标。提供控制因子 (f_t) 时：

\[
r_{i,t}=\alpha_i^{ctrl}+\beta_i^{ctrl}b_t+\gamma_i^\top f_t+\epsilon_{i,t}.
\]

`controlled_beta` 是基准列系数，`residual_alpha` 是截距的年化值。标准误使用预注册的 Newey–West HAC lag。

## 下行与尾部指标

- `downside_beta`：仅在 (b_t<0) 的日期估计带截距一元回归斜率。
- `tail_10_beta`：仅在基准收益位于样本最差10%的日期估计。
- `tail_5_beta`：仅在最差5%日期估计，小样本时只作诊断。
- `common_loss_rate`：基准亏损日中候选或组合同时亏损的比例。

这些指标用于评估，不参与 EMA20 标准成员排名。

## 等权线性袖套

\[
r_{p,t}=\sum_iw_ir_{i,t},\qquad w_i=1/N.
\]

在同一日历、同一基准和同一回归规格下，带截距 OLS 的组合 Beta 应等于成员 Beta 的等权平均。将这一线性关系作为必须通过的数值测试。

线性袖套不等于股票层联合账户。候选之间的重复持股、成交冲突、资金占用和股票票数不在线性叠加中重新模拟。

## 结论语义

- `infeasible`：数据、Top100 或 N 不能形成冻结组合。
- `diagnostic_only`：只有已消费历史证据。
- `research_candidate_pending_forward`：多个冻结滚动期支持继续跟踪，但仍等待新前瞻数据。
- `forward_supported`：只能在预注册、未消费的前瞻数据完成后使用。

低 Beta 不能创造 Alpha。必须同时观察收益保留、正控制后 Alpha、下行/尾部风险和交易活动度，不得只根据普通 Beta 降低宣称成功。
