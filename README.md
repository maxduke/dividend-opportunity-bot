# A股 / ETF 红利机会监控 Telegram Bot

这是一个红利机会监控 Telegram 机器人：将红利指数估值、可交易资产的长期价格位置和 RSI6 短期节奏组合为 Opportunity Score。RSI6 是内部技术因子，不是独立的 RSI 规则产品。

## ✨ 功能特性

- **红利机会监控**: 将可交易资产技术指标与独立 benchmark 指数估值分离，计算 0–100 Opportunity Score。
- **收盘前简报**: 默认在每个交易日 14:50，向开启了此功能的用户推送红利机会评分。
- **白名单**: 只有授权的 Telegram 用户才能与机器人交互。
- **开盘监控**: 任务仅在中国 A 股交易日及交易时段内运行，自动处理节假日和调休。
- **高效缓存**: 资产名称和历史数据均有缓存机制，最大限度减少API调用。
- **健壮性设计**: 包含随机延迟、请求间隔、失败重试与管理员警报等多种机制。
- **Docker化**: 提供优化的 `Dockerfile` 和 `docker-compose.yml`，实现一键启动。
- **CI/CD**: 集成 GitHub Actions，测试通过后自动构建并推送多架构镜像到 GHCR。

## 🤖 机器人命令

- `/start` - 开始使用机器人。
- `/help` - 获取帮助信息。
- `/briefing on|off` - 开启或关闭您的每日收盘前简报。

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
- `/refresh` - 清空历史数据与失败冷却缓存。
- `/proxy_status [refresh]` - 查看或刷新付费 proxy 健康状态。

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
| `RSI_PERIOD`                  | Opportunity 的 RSI 技术因子周期。 | `6`    |
| `TECHNICAL_HISTORY_DAYS`      | 获取 MA200 / 52 周指标的自然日窗口. | `550` |

### 高级配置

| 环境变量                   | 描述                                                         | 默认值     |
| -------------------------- | ------------------------------------------------------------ | ---------- |
| `REQUEST_INTERVAL_SECONDS` | 每个API请求之间的固定间隔时间（秒），用于防止接口限制。     | `1.0`      |
| `FETCH_FAILURE_THRESHOLD`  | 连续获取数据失败多少次后，向管理员发送一条警报通知。         | `5`        |
| `DAILY_BRIEFING_TIMES`      | 每日简报的发送时间 (上海时间, 24小时制)。支持多个，用逗号分隔；留空可全局关闭。 | `14:50` |
| `FETCH_RETRY_ATTEMPTS` | 获取数据失败后的重试次数。     | `3`      |
| `FETCH_RETRY_DELAY_SECONDS`  | 每次重试之间的等待时间（秒）。         | `5`        |
| `AKSHARE_CALL_TIMEOUT_SECONDS` | 单次 AKShare 阻塞调用的异步等待上限；超时后进入已有 retry/fallback。 | `15` |
| `AKSHARE_PROXY_CALL_TIMEOUT_SECONDS` | proxy 模式下 EastMoney 调用的等待上限。 | `300` |
| `HISTORY_FAILURE_COOLDOWN_MINUTES` | 历史数据连续失败后的冷却时间，避免监控循环重复请求。 | `30` |
| `AKSHARE_PROXY_BALANCE_CACHE_MINUTES` | 付费 proxy 余额检查的内存缓存时间。 | `30` |
| `AKSHARE_PROXY_LOW_BALANCE_THRESHOLD` | 正数余额预警阈值；`0` 仅在余额非正或无效时告警。 | `0` |
| `VALUATION_CACHE_HOURS` | 同一 benchmark 的估值缓存时间。 | `12` |
| `BOND_CACHE_HOURS` | 所有规则共享的中国十年期国债缓存时间。 | `12` |
| `VALUATION_STALE_MAX_TRADING_DAYS` | 估值日期超过该交易日数后降级为 WATCH。 | `3` |
| `VALUATION_PERCENTILE_MIN_OBS` | 启用历史估值分位所需的最少样本数。 | `252` |
| `VALUATION_PERCENTILE_MIN_SPAN_YEARS` | 启用成熟历史估值分位所需的最短实际历史跨度（年）。 | `2.0` |
| `VALUATION_PERCENTILE_LOOKBACK_YEARS` | 估值分位回看年数。 | `5` |
| `CSI_DIVIDEND_YIELD_FIELD` | 评分使用 `股息率1` 或 `股息率2`。 | `股息率2` |
| `OPPORTUNITY_ALERT_THRESHOLD` | `/addop` 未指定阈值时使用的告警阈值。 | `60` |
| `MIN_VALUATION_SCORE_FOR_OPPORTUNITY` | 达到 MODERATE 及以上所需的估值最低分。 | `20` |
| `OPPORTUNITY_ALERT_COOLDOWN_MINUTES` | 普通机会告警冷却时间。 | `240` |
| `OPPORTUNITY_MAX_ALERTS_PER_DAY` | 每条机会规则每日普通告警上限；等级升级可覆盖。 | `1` |

场外联接基金的默认模式是 `ENABLE_INTRADAY_MONITOR=false`：不会注册 60 秒循环任务，仅在 14:50 简报或手动 `/opcheck` 时计算。场内 ETF 需要盘中告警时将该开关设为 `true`。简报还需用户执行一次 `/briefing on`。

Opportunity Score 的 V1 权重固定为：估值 50 分（股息率 30、股息率 - 中国十年期国债利差 20）、长期价格位置 30 分（MA200 20、52 周回撤 10）、战术时机 20 分（RSI6 20）。本版本不调权重；历史回放工具用于提供后续 V2 评估证据。

```bash
python scripts/analyze_scoring.py --asset 515180 --benchmark 000922 --db /app/data/rules.db
```

回放从数据库已有的 CSI 估值和 CN10Y 历史起步，默认只获取适合历史研究且价格基准稳定的 `hfq` 日线；`--price-adjust qfq` 可用于和运行时对照诊断，但 qfq 会在后续公司行动后重述历史价格。回放只输出分数分布、相关性和 forward-return 描述，不会修改运行时权重。

Opportunity 监控始终使用前复权（`qfq`）价格。原始价格仅可用于展示或数据源诊断，不参与 MA200、52 周回撤或 RSI6 评分。

### 可选的东方财富代理

项目默认锁定 AKShare `1.18.87`。如果部署环境频繁触发东方财富限流，可安装并显式开启 `akshare-proxy-patch`：

- `ENABLE_AKSHARE_PROXY_PATCH=false`：默认关闭。
- 开启时必须设置 `AKSHARE_PROXY_AUTH_TOKEN`；`AKSHARE_PROXY_AUTH_IP` 默认是服务商文档中的 `101.201.173.125`，这里只填写 IP，不要填写端口。token 只放在部署环境，不要提交到仓库。
- `AKSHARE_PROXY_HOOK_DOMAINS` 只列出需要代理的东方财富域名；新浪、中证、ChinaBond 请求不经过这个补丁。
- 按服务商官方示例默认 `AKSHARE_PROXY_RETRY=30`，并强制 `fast=False`；重试次数可配置，失败请求的计费规则以服务商当前条款为准。
- 启动时会先检查余额；只有有限正数余额才安装补丁。余额不足、响应无效或无法验证时，Bot 继续以安全降级模式启动并只通知管理员。补丁未在启动时安装时，后续充值需要重启 Bot；已安装的进程可在余额缓存过期后自动恢复 qfq 请求。
- proxy 模式下，股票和不复权历史先尝试新浪；前复权 ETF 每日只请求一次 EastMoney。历史失败、估值和国债快照均有缓存，监控循环不会每 60 秒重复调用。
- `fund_name_em()` 使用 `.js` 资源，而补丁会明确绕过 `.js`/`.html`；proxy 模式不调用它，ETF 名称回退为代码，避免产生未代理请求。

补丁会在主程序导入 AKShare 前安装；补丁包不可用或凭据缺失会让启动失败。它是第三方付费代理服务，不是 AKShare 官方组件。请避免在日志、命令行和仓库中暴露长期 token。

余额查询目前由服务商通过 **HTTP** 提供，并把可重复使用的 token 放在 URL 中。因此网络中间设备、服务商访问日志和反向代理理论上都可能看到 token。仅在信任网络路径并接受该风险时启用；代码和通知不会记录 URL、响应正文或 token。如果服务商以后提供 HTTPS 并支持 header/body 凭据，应立即优先迁移到该方式。

## 🚀 部署与运行

**数据库与升级提示**: 不要删除或重新创建旧的数据库文件 (`./data/rules.db`)。启动时会通过 `CREATE TABLE IF NOT EXISTS` 和增量字段迁移创建 Opportunity 表，原 `rules` / `whitelist` 数据及已有 Opportunity 数据都会保留。

1.  **安装 Docker 和 Docker Compose**。
2.  **克隆本仓库**。
3.  **创建并配置 `.env` 文件**。
4.  **首次部署准备数据目录**（镜像使用固定的 `appuser` UID/GID `10001:10001`）:
    ```bash
    mkdir -p data
    sudo chown -R 10001:10001 data
    chmod 700 data
    ```
5.  **启动机器人**:
    ```bash
    docker-compose up -d --build
    ```
6.  **查看日志**: `docker-compose logs -f`
7.  **停止服务**: `docker-compose down`

已有部署升级到固定 UID 镜像时，只需迁移一次数据目录所有权；不要删除 `rules.db`:

```bash
docker compose down
sudo chown -R 10001:10001 ./data
docker compose up -d --build
```

应用在打开 SQLite 前会检查 `/app/data` 是否存在且可写；权限不匹配会立即报错，并提示将 bind-mounted 目录所有权改为 `10001:10001`，不会静默切换到其他数据库路径，也不会在容器内替宿主机执行 `chmod`/`chown`。

## Database backup

`/app/data/rules.db` 保存逐渐累积的本地历史，包括 CSI 估值、中国十年期国债、Opportunity 快照和历史规则数据。升级或迁移权限前请先备份该文件；请使用文件系统 / NAS 快照，或定期复制它进行备份。机器人不会在应用内自动执行备份。

## 数据源与风险说明

- ETF / 股票价格：AKShare `1.18.87`；默认 EastMoney → 新浪 fallback。proxy 开启后，股票或不复权历史优先使用新浪；前复权 ETF 必须通过 EastMoney 获取，确保技术指标口径正确。历史请求按日缓存。
- CSI Index Valuation：通过 AKShare 获取中证指数估值，保存 PE1 / PE2、股息率1 / 股息率2 的全部有效日期。
- China 10Y：优先 ChinaBond（`bond_china_yield`），失败时使用 Sina fallback。

中证估值接口只保证近期数据。Bot 不假设首次运行拥有 5 年历史，而是将上游返回的数据持久化并在后续运行中累积本地历史。

资产与 benchmark 的对应关系由用户在 `/addop` 中指定，Bot 只验证两端数据可用，不验证基金实际跟踪关系。错误配对会让估值分数失去意义，请以基金合同或管理人资料为准。

首次部署可运行 `python scripts/verify_data_sources.py` 验证实时数据源；启用 proxy 后可用 `python scripts/verify_data_sources.py --proxy-only --timeout 300` 逐个验证可 hook 的 EastMoney 接口。proxy smoke 会阻断补丁自带的目标域名直连 fallback，确保显示 `route=PATCH` 时请求确实由代理返回。普通 CI 不访问真实 AKShare，只有设置 `RUN_LIVE_AKSHARE_TESTS=1` 才执行 live smoke test。

Opportunity Score 是量化监控信号，不是投资建议，也不预测市场底部。
