# CLAUDE.md

## 项目概述

A 股 / ETF RSI(6) 与红利 Opportunity Score 监控 Telegram 机器人。旧 RSI 规则保持兼容；Opportunity Monitor 将指数估值、资产长期价格位置和 RSI 短期节奏组合为可解释评分。

**技术栈**: Python 3.12+ / python-telegram-bot / akshare / pandas / SQLite / Docker

## 模块结构

```
src/
├── main.py            # 入口：配置验证 → DB 初始化 → 注册 handlers/jobs → run_polling
├── config.py          # 环境变量加载、值范围验证、常量定义、日志配置
├── database.py        # SQLite 持久连接 + threading.Lock 保护，白名单 CRUD
├── data_fetcher.py    # 数据获取（东方财富/新浪双源容灾）、RSI 计算、缓存
├── provider_bootstrap.py # 可选 akshare-proxy-patch 启动配置
├── valuation_fetcher.py # 中证估值与中国十年国债获取、缓存、持久化
├── metrics.py         # Opportunity 指标与评分纯函数
├── scoring_config.py  # 集中的评分阈值
├── opportunity.py     # Opportunity 评估、门控、消息格式与告警判断
├── market.py          # XSHG 交易日历与超出覆盖期的 AKShare 按日回退
├── handlers.py        # 所有 Telegram 命令处理器（@whitelisted_only/@admin_only）
├── jobs.py            # 后台定时任务：check_rules_job、daily_briefing_job
└── utils.py           # 共享工具：normalize_hist_df()、get_sina_symbol()
```

## 开发环境

```bash
# 创建 venv（使用 uv）
uv venv .venv --python 3.12
source .venv/bin/activate

# 安装依赖
uv pip install -r requirements.txt

# 安装测试依赖
uv pip install pytest

# 运行
TELEGRAM_TOKEN=xxx ADMIN_USER_ID=123 python -m src.main

# 运行测试
pytest tests/ -v

# 手动 live 数据源验证（会访问外部接口；代理请求可能计费）
python scripts/verify_data_sources.py --timeout 300

# Docker 构建
docker-compose up -d --build
```

## 依赖管理

- `requirements.in` — 顶层依赖声明（未锁定版本）
- `requirements.txt` — pip-compile 生成的锁定文件
- 更新依赖：`./upgrade_requirments.sh`（Docker 内 pip-compile）
- CI 每周自动更新 akshare：`.github/workflows/update-akshare-pr.yml`

## 代码规范

- 所有用户可见文本使用中文
- 日志消息使用中文，log level：INFO 正常流程，WARNING 可恢复错误，ERROR 不可恢复
- 外部 API 调用必须经过 `_run_with_retries()` 包装（指数退避）
- DataFrame 空值检查统一使用 `if df is None or df.empty:`，不要用 `if not df:`
- 价格的 None 检查使用 `if price is None:`，不要用 `if not price:`（0.0 是合法价格）
- pandas NaN 检查使用 `pd.isna()`，不要用 `is None`
- 数据库操作通过 `database.db_execute()` 统一入口，自带锁保护
- 全局可变状态（缓存等）必须使用 `asyncio.Lock` 或 `threading.Lock` 保护

## 关键业务逻辑

- **RSI 算法**: Wilder 平滑（EWM alpha=1/N），复刻同花顺/东财口径，见 `data_fetcher.calculate_rsi_exact()`
- **复权处理**: 计算复权因子（复权收盘/未复权收盘），将实时价格转换到复权尺度
- **数据源容灾**: 东方财富请求失败后自动切换新浪；proxy 模式下优先使用免费源
- **通知去重**: 进入区间后发送 N 次通知（可配），离开区间自动重置计数器
- **机会评分门控**: 估值缺失、过期或估值分过低时不得升级到 MODERATE 以上
- **请求成本控制**: 技术历史每日缓存；估值和国债默认缓存 12 小时；代理请求不叠加应用层重试

## 测试

```bash
pytest tests/ -v
```

普通测试完全离线，覆盖 RSI、Opportunity 指标与边界、门控、告警、数据库迁移、provider 归一化及请求成本控制。真实 AKShare 验证只通过 `scripts/verify_data_sources.py` 手动执行或由显式启用的 CI live test 执行。
