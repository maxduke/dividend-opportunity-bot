# A股 / ETF 技术指标与红利机会监控 Telegram Bot

这是一个 Telegram 机器人，用于监控中国 A 股 / ETF 的 RSI 指标，以及基于红利指数估值、长期价格位置和短期 RSI 的 Opportunity Score。

Legacy RSI monitoring remains supported；升级不会删除已有 RSI 规则。

## ✨ 功能特性

- **多规则监控**: 支持为同一个资产设置多个不同的RSI监控区间。
- **即时查询**: 用户可随时使用 `/check` 命令，获取所有监控资产的最新RSI值。
- **红利机会监控**: 将可交易资产技术指标与独立 benchmark 指数估值分离，计算 0–100 Opportunity Score。
- **收盘前简报**: 默认在每个交易日 14:50，向开启了此功能的用户推送 RSI 与红利机会评分。
- **白名单**: 只有授权的 Telegram 用户才能与机器人交互。
- **开盘监控**: 任务仅在中国 A 股交易日及交易时段内运行，自动处理节假日和调休。
- **高效缓存**: 资产名称和历史数据均有缓存机制，最大限度减少API调用。
- **健壮性设计**: 包含随机延迟、请求间隔、失败重试与管理员警报等多种机制。
- **Docker化**: 提供优化的 `Dockerfile` 和 `docker-compose.yml`，实现一键启动。
- **CI/CD**: 集成 GitHub Actions，测试通过后自动构建并推送多架构镜像到 GHCR。

## 🤖 机器人命令

- `/start` - 开始使用机器人。
- `/help` - 获取帮助信息。
- `/check` - 立即查询您所有激活规则的当前RSI值。
- `/briefing on|off` - 开启或关闭您的每日收盘前简报。
- `/add <code> <min> <max>` - 添加一条监控规则。
- `/del <id>` - 删除一条规则。
- `/list` - 查看您的所有监控规则。
- `/on <id>` - 开启一条规则。
- `/off <id>` - 关闭一条规则。

### Opportunity Monitor 命令

- `/addop <asset_code> <benchmark_code> [min_score]` - 添加机会监控；例如 `/addop 515180 000922 70`。
- `/delop <id>` - 删除机会监控。
- `/oplist` - 查看机会监控。
- `/opon <id>` / `/opoff <id>` - 开启 / 关闭机会监控。
- `/opcheck [id]` - 查询完整分数明细；估值和国债数据遵守缓存 TTL。

### (管理员命令)

- `/add_w <user_id>` - 添加用户到白名单。
- `/del_w <user_id>` - 从白名单移除用户。
- `/list_w` - 查看白名单列表（会显示简报开启状态）。

## ⚙️ 配置

通过在项目根目录创建 `.env` 文件来配置机器人。

Docker 部署请保留 `.env.example` 中的 `DB_FILE=/app/data/rules.db`，确保数据库写入持久化卷。本地直接运行时可改为 `DB_FILE=rules.db`。

### 基础配置

| 环境变量         | 描述                               | 示例值                                     |
| ---------------- | ---------------------------------- | ------------------------------------------ |
| `TELEGRAM_TOKEN` | **必需**. Telegram Bot Token.      | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `ADMIN_USER_ID`  | **必需**. 你的 Telegram User ID.   | `123456789`                                |

### 监控参数配置

| 环境变量                      | 描述                               | 默认值 |
| ----------------------------- | ---------------------------------- | ------ |
| `CHECK_INTERVAL_SECONDS`      | 启用盘中监控时的规则检查间隔（秒）。 | `60`   |
| `ENABLE_INTRADAY_MONITOR`     | 是否启用盘中循环及自动告警；场外联接基金建议关闭，场内 ETF 可开启。 | `false` |
| `RSI_PERIOD`                  | 计算RSI指标的周期.                 | `6`    |
| `USE_ADJUST`                  | 是否使用前复权价格 (`true`=是, `false`=否)。启用后，会将实时价格按最新交易日的复权因子转换为复权尺度。 | `true` |
| `HIST_FETCH_DAYS`             | 获取用于计算RSI的历史数据的天数.   | `200`   |
| `TECHNICAL_HISTORY_DAYS`      | 获取 MA200 / 52 周指标的自然日窗口. | `550` |
| `MAX_NOTIFICATIONS_PER_TRIGGER` | 每个上海自然日内、单条规则处于触发区间时发送通知的最大次数；次日会自动重置。 | `1`    |

### 高级配置

| 环境变量                   | 描述                                                         | 默认值     |
| -------------------------- | ------------------------------------------------------------ | ---------- |
| `RANDOM_DELAY_MAX_SECONDS` | 在每次检查周期开始时，增加一个0到该秒数之间的随机延迟。     | `0`        |
| `REQUEST_INTERVAL_SECONDS` | 每个API请求之间的固定间隔时间（秒），用于防止接口限制。     | `1.0`      |
| `FETCH_FAILURE_THRESHOLD`  | 连续获取数据失败多少次后，向管理员发送一条警报通知。         | `5`        |
| `ENABLE_DAILY_BRIEFING`    | **每日简报的主开关**。设为 `true` 以允许用户使用此功能。     | `true`    |
| `DAILY_BRIEFING_TIMES`      | 每日简报的发送时间 (上海时间, 24小时制)。支持多个，用逗号分隔。     | `14:50`    |
| `FETCH_RETRY_ATTEMPTS` | 获取数据失败后的重试次数。     | `3`      |
| `FETCH_RETRY_DELAY_SECONDS`  | 每次重试之间的等待时间（秒）。         | `5`        |
| `AKSHARE_CALL_TIMEOUT_SECONDS` | 单次 AKShare 阻塞调用的异步等待上限；超时后进入已有 retry/fallback。 | `15` |
| `AKSHARE_PROXY_CALL_TIMEOUT_SECONDS` | proxy 模式下 EastMoney 调用的等待上限。 | `300` |
| `HISTORY_FAILURE_COOLDOWN_MINUTES` | 历史数据连续失败后的冷却时间，避免监控循环重复请求。 | `30` |
| `ENABLE_OPPORTUNITY_MONITOR` | Opportunity Monitor 主开关。 | `true` |
| `VALUATION_CACHE_HOURS` | 同一 benchmark 的估值缓存时间。 | `12` |
| `BOND_CACHE_HOURS` | 所有规则共享的中国十年期国债缓存时间。 | `12` |
| `VALUATION_STALE_MAX_DAYS` | 估值日期超过该日历天数后降级为 WATCH。 | `7` |
| `VALUATION_PERCENTILE_MIN_OBS` | 启用历史估值分位所需的最少样本数。 | `252` |
| `VALUATION_PERCENTILE_LOOKBACK_YEARS` | 估值分位回看年数。 | `5` |
| `CSI_DIVIDEND_YIELD_FIELD` | 评分使用 `股息率1` 或 `股息率2`。 | `股息率2` |
| `OPPORTUNITY_ALERT_THRESHOLD` | `/addop` 未指定阈值时使用的告警阈值。 | `60` |
| `MIN_VALUATION_SCORE_FOR_OPPORTUNITY` | 达到 MODERATE 及以上所需的估值最低分。 | `20` |
| `OPPORTUNITY_ALERT_COOLDOWN_MINUTES` | 普通机会告警冷却时间。 | `240` |
| `OPPORTUNITY_MAX_ALERTS_PER_DAY` | 每条机会规则每日普通告警上限；等级升级可覆盖。 | `1` |

场外联接基金的默认模式是 `ENABLE_INTRADAY_MONITOR=false`：60 秒任务会在访问数据源前立即返回，仅在 14:50 简报或手动 `/check`、`/opcheck` 时计算。场内 ETF 需要盘中告警时将该开关设为 `true`。简报还需用户执行一次 `/briefing on`。

### 可选的东方财富代理

项目默认锁定 AKShare `1.18.87`。如果部署环境频繁触发东方财富限流，可安装并显式开启 `akshare-proxy-patch`：

- `ENABLE_AKSHARE_PROXY_PATCH=false`：默认关闭。
- 开启时必须设置 `AKSHARE_PROXY_AUTH_TOKEN`；`AKSHARE_PROXY_AUTH_IP` 默认是服务商文档中的 `101.201.173.125`，这里只填写 IP，不要填写端口。token 只放在部署环境，不要提交到仓库。
- `AKSHARE_PROXY_HOOK_DOMAINS` 只列出需要代理的东方财富域名；新浪、中证、ChinaBond 请求不经过这个补丁。
- 按服务商官方示例默认 `AKSHARE_PROXY_RETRY=30`，并强制 `fast=False`；重试次数可配置，失败请求的计费规则以服务商当前条款为准。
- proxy 模式下，股票和不复权历史先尝试新浪；前复权 ETF 每日只请求一次 EastMoney。历史失败、估值和国债快照均有缓存，监控循环不会每 60 秒重复调用。
- `fund_name_em()` 使用 `.js` 资源，而补丁会明确绕过 `.js`/`.html`；proxy 模式不调用它，ETF 名称回退为代码，避免产生未代理请求。

补丁会在主程序导入 AKShare 前安装，并且只在环境变量明确开启且余额检查成功时生效；补丁包不可用或凭据缺失会让启动失败。余额检查失败时不会安装 patch，并记录不包含 token 的 warning。它是第三方付费代理服务，不是 AKShare 官方组件。

服务商当前的余额与授权接口使用 HTTP `47001`，token 会出现在请求 URL 中。只应在可信网络中启用，并避免在日志、命令行和仓库中暴露长期 token。

## 🚀 部署与运行

**升级提示**: 不要删除旧的数据库文件 (`./data/rules.db`)。启动时会通过 `CREATE TABLE IF NOT EXISTS` 和增量字段迁移创建 Opportunity 表，原 `rules` / `whitelist` 数据会保留。

1.  **安装 Docker 和 Docker Compose**。
2.  **克隆本仓库**。
3.  **创建并配置 `.env` 文件**。
4.  **启动机器人**:
    ```bash
    docker-compose up -d --build
    ```
5.  **查看日志**: `docker-compose logs -f`
6.  **停止服务**: `docker-compose down`

## 数据源与风险说明

- ETF / 股票价格：AKShare `1.18.87`；默认 EastMoney → 新浪 fallback。proxy 开启后，股票或不复权历史优先使用新浪；前复权 ETF 必须通过 EastMoney 获取，确保技术指标口径正确。历史请求按日缓存。
- CSI Index Valuation：通过 AKShare 获取中证指数估值，保存 PE1 / PE2、股息率1 / 股息率2 的全部有效日期。
- China 10Y：优先 ChinaBond（`bond_china_yield`），失败时使用 Sina fallback。

中证估值接口只保证近期数据。Bot 不假设首次运行拥有 5 年历史，而是将上游返回的数据持久化并在后续运行中累积本地历史。

资产与 benchmark 的对应关系由用户在 `/addop` 中指定，Bot 只验证两端数据可用，不验证基金实际跟踪关系。错误配对会让估值分数失去意义，请以基金合同或管理人资料为准。

首次部署可运行 `python scripts/verify_data_sources.py` 验证实时数据源；启用 proxy 后可用 `python scripts/verify_data_sources.py --proxy-only --timeout 300` 逐个验证可 hook 的 EastMoney 接口。proxy smoke 会阻断补丁自带的目标域名直连 fallback，确保显示 `route=PATCH` 时请求确实由代理返回。余额不可确认时不安装 patch，也不继续产生目标接口请求。普通 CI 不访问真实 AKShare，只有设置 `RUN_LIVE_AKSHARE_TESTS=1` 才执行 live smoke test。

Opportunity Score 是量化监控信号，不是投资建议，也不预测市场底部。
