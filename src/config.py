# -*- coding: utf-8 -*-

import logging
import os
import sys

# --- 日志配置 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
for _logger_name in ("httpx", "telegram.ext", "apscheduler"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- 机器人配置 (从环境变量读取) ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_USER_ID_STR = os.getenv('ADMIN_USER_ID')
ADMIN_USER_ID = int(ADMIN_USER_ID_STR) if ADMIN_USER_ID_STR and ADMIN_USER_ID_STR.isdigit() else None
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', '60'))
DB_FILE = os.getenv('DB_FILE', 'rules.db')

# --- 监控参数配置 ---
RSI_PERIOD = int(os.getenv('RSI_PERIOD', '6'))
USE_ADJUST = os.getenv('USE_ADJUST', 'true').lower() == 'true'
HIST_FETCH_DAYS = int(os.getenv('HIST_FETCH_DAYS', '200'))
TECHNICAL_HISTORY_DAYS = int(os.getenv('TECHNICAL_HISTORY_DAYS', '550'))
MAX_NOTIFICATIONS_PER_TRIGGER = int(os.getenv('MAX_NOTIFICATIONS_PER_TRIGGER', '1'))
ENABLE_OPPORTUNITY_MONITOR = os.getenv('ENABLE_OPPORTUNITY_MONITOR', 'true').lower() == 'true'
VALUATION_CACHE_HOURS = float(os.getenv('VALUATION_CACHE_HOURS', '12'))
BOND_CACHE_HOURS = float(os.getenv('BOND_CACHE_HOURS', '12'))
VALUATION_STALE_MAX_DAYS = int(os.getenv('VALUATION_STALE_MAX_DAYS', '7'))
VALUATION_PERCENTILE_MIN_OBS = int(os.getenv('VALUATION_PERCENTILE_MIN_OBS', '252'))
VALUATION_PERCENTILE_LOOKBACK_YEARS = int(os.getenv('VALUATION_PERCENTILE_LOOKBACK_YEARS', '5'))
CSI_DIVIDEND_YIELD_FIELD = os.getenv('CSI_DIVIDEND_YIELD_FIELD', '股息率2')
OPPORTUNITY_ALERT_THRESHOLD = float(os.getenv('OPPORTUNITY_ALERT_THRESHOLD', '60'))
MIN_VALUATION_SCORE_FOR_OPPORTUNITY = float(os.getenv('MIN_VALUATION_SCORE_FOR_OPPORTUNITY', '20'))
OPPORTUNITY_ALERT_COOLDOWN_MINUTES = int(os.getenv('OPPORTUNITY_ALERT_COOLDOWN_MINUTES', '240'))
OPPORTUNITY_MAX_ALERTS_PER_DAY = int(os.getenv('OPPORTUNITY_MAX_ALERTS_PER_DAY', '1'))

# --- 高级配置 ---
RANDOM_DELAY_MAX_SECONDS = float(os.getenv('RANDOM_DELAY_MAX_SECONDS', '0'))
FETCH_FAILURE_THRESHOLD = int(os.getenv('FETCH_FAILURE_THRESHOLD', '5'))
REQUEST_INTERVAL_SECONDS = float(os.getenv('REQUEST_INTERVAL_SECONDS', '1.0'))
ENABLE_DAILY_BRIEFING = os.getenv('ENABLE_DAILY_BRIEFING', 'false').lower() == 'true'
BRIEFING_TIMES_STR = os.getenv('DAILY_BRIEFING_TIMES', '15:30')
FETCH_RETRY_ATTEMPTS = int(os.getenv('FETCH_RETRY_ATTEMPTS', '3'))
FETCH_RETRY_DELAY_SECONDS = int(os.getenv('FETCH_RETRY_DELAY_SECONDS', '5'))
AKSHARE_CALL_TIMEOUT_SECONDS = float(os.getenv('AKSHARE_CALL_TIMEOUT_SECONDS', '15'))
AKSHARE_PROXY_CALL_TIMEOUT_SECONDS = float(os.getenv('AKSHARE_PROXY_CALL_TIMEOUT_SECONDS', '300'))
HISTORY_FAILURE_COOLDOWN_MINUTES = float(os.getenv('HISTORY_FAILURE_COOLDOWN_MINUTES', '30'))
EM_BLOCK_CHECK_INTERVAL_SECONDS = int(os.getenv('EM_BLOCK_CHECK_INTERVAL_SECONDS', '300'))
EM_BLOCK_CHECK_URL = "https://i.eastmoney.com/websitecaptcha/api/checkuser?callback=wsc_checkuser"
ENABLE_AKSHARE_PROXY_PATCH = os.getenv('ENABLE_AKSHARE_PROXY_PATCH', 'false').lower() == 'true'
AKSHARE_PROXY_AUTH_IP = os.getenv('AKSHARE_PROXY_AUTH_IP', '101.201.173.125').strip()
AKSHARE_PROXY_AUTH_TOKEN = os.getenv('AKSHARE_PROXY_AUTH_TOKEN', '').strip()
AKSHARE_PROXY_RETRY = int(os.getenv('AKSHARE_PROXY_RETRY', '1'))
AKSHARE_PROXY_HOOK_DOMAINS = os.getenv(
    'AKSHARE_PROXY_HOOK_DOMAINS',
    'push2.eastmoney.com,push2his.eastmoney.com',
)

# --- 应用内常量 ---
KEY_HIST_CACHE = 'hist_data_cache'
KEY_NAME_CACHE = 'name_cache'
KEY_CACHE_DATE = 'cache_date'
KEY_HIST_FAILURE_CACHE = 'hist_failure_cache'
KEY_FAILURE_COUNT = 'fetch_failure_count'
KEY_FAILURE_SENT = 'failure_notification_sent'
STOCK_PREFIXES = ('0', '3', '6', '4', '8')
ETF_PREFIXES = ('5', '1')
NAME_CACHE_MAX_SIZE = 500


def validate_config():
    """验证关键配置值的合法性，不合法则退出。"""
    errors = []

    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN 未设置")
    if not ADMIN_USER_ID:
        errors.append("ADMIN_USER_ID 未设置或不是合法的正整数")

    if CHECK_INTERVAL_SECONDS <= 0:
        errors.append(f"CHECK_INTERVAL_SECONDS 必须 > 0，当前值: {CHECK_INTERVAL_SECONDS}")
    if RSI_PERIOD <= 0:
        errors.append(f"RSI_PERIOD 必须 > 0，当前值: {RSI_PERIOD}")
    if HIST_FETCH_DAYS <= RSI_PERIOD:
        errors.append(f"HIST_FETCH_DAYS({HIST_FETCH_DAYS}) 必须 > RSI_PERIOD({RSI_PERIOD})")
    if TECHNICAL_HISTORY_DAYS <= RSI_PERIOD:
        errors.append(
            f"TECHNICAL_HISTORY_DAYS({TECHNICAL_HISTORY_DAYS}) 必须 > RSI_PERIOD({RSI_PERIOD})"
        )
    if VALUATION_CACHE_HOURS <= 0:
        errors.append(f"VALUATION_CACHE_HOURS 必须 > 0，当前值: {VALUATION_CACHE_HOURS}")
    if BOND_CACHE_HOURS <= 0:
        errors.append(f"BOND_CACHE_HOURS 必须 > 0，当前值: {BOND_CACHE_HOURS}")
    if VALUATION_STALE_MAX_DAYS < 0:
        errors.append(f"VALUATION_STALE_MAX_DAYS 必须 >= 0，当前值: {VALUATION_STALE_MAX_DAYS}")
    if VALUATION_PERCENTILE_MIN_OBS < 1:
        errors.append(f"VALUATION_PERCENTILE_MIN_OBS 必须 >= 1，当前值: {VALUATION_PERCENTILE_MIN_OBS}")
    if VALUATION_PERCENTILE_LOOKBACK_YEARS < 1:
        errors.append(
            f"VALUATION_PERCENTILE_LOOKBACK_YEARS 必须 >= 1，当前值: {VALUATION_PERCENTILE_LOOKBACK_YEARS}"
        )
    if CSI_DIVIDEND_YIELD_FIELD not in ('股息率1', '股息率2'):
        errors.append(f"CSI_DIVIDEND_YIELD_FIELD 必须是 股息率1 或 股息率2，当前值: {CSI_DIVIDEND_YIELD_FIELD}")
    if not 0 <= OPPORTUNITY_ALERT_THRESHOLD <= 100:
        errors.append(f"OPPORTUNITY_ALERT_THRESHOLD 必须在 0 到 100 之间，当前值: {OPPORTUNITY_ALERT_THRESHOLD}")
    if not 0 <= MIN_VALUATION_SCORE_FOR_OPPORTUNITY <= 50:
        errors.append(
            "MIN_VALUATION_SCORE_FOR_OPPORTUNITY 必须在 0 到 50 之间，"
            f"当前值: {MIN_VALUATION_SCORE_FOR_OPPORTUNITY}"
        )
    if OPPORTUNITY_ALERT_COOLDOWN_MINUTES < 0:
        errors.append(
            f"OPPORTUNITY_ALERT_COOLDOWN_MINUTES 必须 >= 0，当前值: {OPPORTUNITY_ALERT_COOLDOWN_MINUTES}"
        )
    if OPPORTUNITY_MAX_ALERTS_PER_DAY < 1:
        errors.append(f"OPPORTUNITY_MAX_ALERTS_PER_DAY 必须 >= 1，当前值: {OPPORTUNITY_MAX_ALERTS_PER_DAY}")
    if REQUEST_INTERVAL_SECONDS < 0:
        errors.append(f"REQUEST_INTERVAL_SECONDS 必须 >= 0，当前值: {REQUEST_INTERVAL_SECONDS}")
    if FETCH_RETRY_ATTEMPTS < 1:
        errors.append(f"FETCH_RETRY_ATTEMPTS 必须 >= 1，当前值: {FETCH_RETRY_ATTEMPTS}")
    if AKSHARE_CALL_TIMEOUT_SECONDS <= 0:
        errors.append(f"AKSHARE_CALL_TIMEOUT_SECONDS 必须 > 0，当前值: {AKSHARE_CALL_TIMEOUT_SECONDS}")
    if AKSHARE_PROXY_CALL_TIMEOUT_SECONDS <= 0:
        errors.append(
            "AKSHARE_PROXY_CALL_TIMEOUT_SECONDS 必须 > 0，"
            f"当前值: {AKSHARE_PROXY_CALL_TIMEOUT_SECONDS}"
        )
    if HISTORY_FAILURE_COOLDOWN_MINUTES < 0:
        errors.append(
            "HISTORY_FAILURE_COOLDOWN_MINUTES 必须 >= 0，"
            f"当前值: {HISTORY_FAILURE_COOLDOWN_MINUTES}"
        )
    if MAX_NOTIFICATIONS_PER_TRIGGER < 1:
        errors.append(f"MAX_NOTIFICATIONS_PER_TRIGGER 必须 >= 1，当前值: {MAX_NOTIFICATIONS_PER_TRIGGER}")
    if ENABLE_AKSHARE_PROXY_PATCH:
        if not AKSHARE_PROXY_AUTH_TOKEN:
            errors.append("ENABLE_AKSHARE_PROXY_PATCH=true 时必须设置 AKSHARE_PROXY_AUTH_TOKEN")
        if AKSHARE_PROXY_RETRY < 1:
            errors.append(f"AKSHARE_PROXY_RETRY 必须 >= 1，当前值: {AKSHARE_PROXY_RETRY}")
        if not any(domain.strip() for domain in AKSHARE_PROXY_HOOK_DOMAINS.split(',')):
            errors.append("AKSHARE_PROXY_HOOK_DOMAINS 至少需要一个域名")

    if errors:
        for err in errors:
            logger.critical(f"配置错误: {err}")
        sys.exit(1)


def log_config():
    """在启动时打印当前配置。"""
    logger.info("--- 机器人配置 ---")
    logger.info(f"RSI 周期: {RSI_PERIOD}")
    logger.info(f"历史数据天数: {HIST_FETCH_DAYS}")
    logger.info(f"技术指标历史数据天数: {TECHNICAL_HISTORY_DAYS}")
    logger.info(f"是否复权: {USE_ADJUST}")
    logger.info(f"每日最大通知次数/规则: {MAX_NOTIFICATIONS_PER_TRIGGER}")
    logger.info(f"检查间隔: {CHECK_INTERVAL_SECONDS}秒")
    logger.info(f"数据库文件: {DB_FILE}")
    logger.info(f"机会监控主开关: {'开启' if ENABLE_OPPORTUNITY_MONITOR else '关闭'}")
    logger.info(f"估值缓存: {VALUATION_CACHE_HOURS}小时，国债缓存: {BOND_CACHE_HOURS}小时")
    logger.info(f"估值字段: {CSI_DIVIDEND_YIELD_FIELD}，机会告警阈值: {OPPORTUNITY_ALERT_THRESHOLD}")
    logger.info(
        f"估值过期上限: {VALUATION_STALE_MAX_DAYS}天，分位样本: {VALUATION_PERCENTILE_MIN_OBS}，"
        f"回看: {VALUATION_PERCENTILE_LOOKBACK_YEARS}年"
    )
    logger.info(
        f"机会最低估值分: {MIN_VALUATION_SCORE_FOR_OPPORTUNITY}，"
        f"冷却: {OPPORTUNITY_ALERT_COOLDOWN_MINUTES}分钟，每日上限: {OPPORTUNITY_MAX_ALERTS_PER_DAY}"
    )
    logger.info(f"最大随机延迟: {RANDOM_DELAY_MAX_SECONDS}秒")
    logger.info(f"失败通知阈值: {FETCH_FAILURE_THRESHOLD}次")
    logger.info(f"请求间隔: {REQUEST_INTERVAL_SECONDS}秒")
    logger.info(f"AKShare单次调用超时: {AKSHARE_CALL_TIMEOUT_SECONDS}秒")
    logger.info(
        f"AKShare代理调用超时: {AKSHARE_PROXY_CALL_TIMEOUT_SECONDS}秒，"
        f"历史失败冷却: {HISTORY_FAILURE_COOLDOWN_MINUTES}分钟"
    )
    logger.info(
        f"AKShare代理补丁: {'开启' if ENABLE_AKSHARE_PROXY_PATCH else '关闭'}"
        + (f"，内部重试: {AKSHARE_PROXY_RETRY}，快速替换: 关闭" if ENABLE_AKSHARE_PROXY_PATCH else "")
    )
    logger.info(f"每日简报主开关: {'开启' if ENABLE_DAILY_BRIEFING else '关闭'}")
    if ENABLE_DAILY_BRIEFING:
        logger.info(f"每日简报发送时间: {BRIEFING_TIMES_STR} (上海时间)")
    logger.info("--------------------")
