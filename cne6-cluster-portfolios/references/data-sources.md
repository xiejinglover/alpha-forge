# CNE6 数据来源与获取

## 先发现数据，再决定是否导出

需要两类不同数据：个股风格暴露用于聚合策略暴露；日因子收益用于风险协方差。`optimize-strategy-portfolios` 内置控制因子收益只有第二类数据，不能代替第一类，也不能未经列顺序与口径核对直接作为本技能输入。

优先在目标仓库配置、数据清单和已发布产物中查找 `hermes_cne6_sw21_v1`、`candidate_annual_exposures`、`factor_returns.parquet`。已有年度候选暴露只有在候选、原生选股规则、因子与日期版本一致且覆盖可验证时才能复用。

历史服务器参考缓存：

```text
/data/zzh/ZyQuant/data/reference_factors/hermes_cne6_sw21_v1/as_of=2026-08-31/
  factor_returns.parquet
  exposures/year=YYYY/part-000.parquet
```

这是远端服务器的历史位置，不是安装技能后本机会自动存在的文件。通过用户已有服务器访问方式检查清单、哈希与实际覆盖；在目标项目配置 `reference_factor_root` 或等价路径，不把此路径写进新代码。技能不内置个股暴露，也不自动搬运数据库副本。

## Hermes 来源和字段

现有来源为 ClickHouse 数据库 `hermes`：

| 表 | 用途 | 逻辑键 |
|---|---|---|
| `dy1d_exposure_cne6_sw21` | 股票每日风格暴露 | `TRADE_DATE, TICKER_SYMBOL` |
| `dy1d_factor_ret_cne6_sw21` | 每日风格因子收益 | `TRADE_DATE` |

八维默认顺序及映射如下；协方差和暴露必须用同一顺序：

| 供应商字段 | 缓存字段 | 含义 |
|---|---|---|
| `BETA` | `beta` | 市场 Beta 风格 |
| `SIZE` | `size` | 规模 |
| `MOMENTUM` | `momentum` | 动量 |
| `BTOP` | `book_to_price` | 账面市值比 |
| `RESVOL` | `residual_volatility` | 残差波动 |
| `LIQUIDTY` | `liquidity` | 流动性，供应商字段拼写如此 |
| `PROFIT` | `profitability` | 盈利能力 |
| `INVSQLTY` | `investment_quality` | 投资质量 |

`COUNTRY` 是历史回归控制列之一，不属于本技能默认八维聚类。供应商表还含其他风格；更改因子集合时必须同时取得对应股票暴露和日因子收益。

缓存使用 `trade_date`（日期）、`ticker_symbol`（字符串，保留前导零）、上述浮点因子列。暴露是供应商风格单位，不是股价，无需复权；不能声称已重建供应商的标准化和组件权重。导出脚本仅转 Float64，不做百分数/小数转换；日因子收益单位需依供应商元数据或已有已验证契约核对并记录，不能只凭数值大小猜测。股票交易日期按中国市场 Asia/Shanghai 日历处理；日期字段本身不提供发布时间。记录证券市场和币种口径，不把因子暴露误作货币金额。

## 导出入口与凭据

历史导出器可在源码中检索 `tools/cache_hermes_cne6_reference.py`。本机研究副本位于 `/Users/xj/Documents/EMA20动量/server_patch/tools/cache_hermes_cne6_reference.py`，这是溯源线索，不是可移植依赖。

该脚本接受 `--root`、`--as-of YYYY-MM-DD`、`--endpoint`、`--database`、`--user`、`--password-stdin`。连接默认值可在源码发现；实际调用使用目标环境配置的地址与账号，不依赖旧默认值。凭据从预先配置的 `HERMES_CLICKHOUSE_PASSWORD` 读取，或用 `--password-stdin` 从标准输入提供。不要把密码写入技能、命令行参数、日志或清单。

下面仅为核对并适配导出器后的调用模板；环境变量须先由目标环境提供：

```bash
python3 "$CNE6_EXPORT_SCRIPT" \
  --root "$CNE6_CACHE_ROOT" \
  --as-of "$CNE6_AS_OF" \
  --endpoint "$HERMES_CLICKHOUSE_ENDPOINT" \
  --database hermes \
  --user "$HERMES_CLICKHOUSE_USER"
```

原脚本从 2008 年开始导出至指定日期，且同时导出市场收益。它硬编码了历史验收行数：因子收益 4,536、个股暴露 15,429,691、市场收益 25,844；更换截止日或数据修订后不能机械沿用。先检查范围、实际元数据和验收逻辑，必要时在目标仓库适配现有导出器，不只删除验证让导出通过。它使用暂存、验证、不可变目录发布流程，已有同名快照不能覆盖。

## 去重、连接与时间边界

历史查询对逻辑键使用 `row_number()`，按 `_version DESC, UPDATE_TIME DESC, ID DESC` 取最新记录，再过滤 `_is_deleted=0`。不要先删掉删除标志行再取旧记录，否则可能复活已删除数据。暴露按交易年分区，交易日期限定在请求范围内。

这套查询只限制 `TRADE_DATE`，没有限制记录修订时间必须早于历史决策时刻。目录的 `as_of` 是交易日期截止标签，不是历史数据库快照保证；后续重新导出相同截止日也可能得到修订后的值。严格 PIT 需要供应商版本历史/发布时间或当时保存的快照，且只使用决策时已可得版本；无法取得时保留最新修订数据的诊断身份，报告可能的修订偏差。

将策略 `instrument_id` 映射到供应商 `ticker_symbol` 前验证市场、交易所后缀、前导零和唯一性，不盲目截取六位。按信号日期匹配股票暴露并验证可得时刻：同日收盘暴露不能用于当天收盘前决策。年度冻结只用截止日前已可得的训练资料；若存在发布时间滞后，依实际可得时刻排除，不能补用未来数据。

先检查日期范围、唯一键、有限值、预测股票映射、有效信号日覆盖、账户与因子交易日历及文件哈希。暴露不补零、不跨日填充；缺失导致无效原生日，并按方法文档的固定分母计算覆盖。因子收益缺失不得静默缩短共同日历；先解决来源缺口，否则该批次风险转换不可执行。

保存来源表、查询/脚本版本、下载时间、请求截止日、实际最小最大日期、行数、字段与单位、文件 SHA256、删除和修订处理、PIT 限制。技能迁移到其他供应商时重新核对语义和覆盖，不能仅将同名字段改名后视为等价 CNE6。
