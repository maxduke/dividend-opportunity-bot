# -*- coding: utf-8 -*-

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import pandas_market_calendars as mcal

CHINA_CALENDAR = mcal.get_calendar("XSHG")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

_LOCAL_HOLIDAYS = CHINA_CALENDAR.adhoc_holidays
LOCAL_CALENDAR_COVERAGE_START = _LOCAL_HOLIDAYS.min().date()
LOCAL_CALENDAR_COVERAGE_END = _LOCAL_HOLIDAYS.max().date()
_trade_day_cache = {"days": None, "loaded_on": None}


def _load_trade_days_from_ak() -> set | None:
    try:
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return None
        date_col = "trade_date" if "trade_date" in df.columns else "日期"
        if date_col not in df.columns:
            return None
        return set(pd.to_datetime(df[date_col]).dt.date)
    except Exception as exc:
        logger.warning("从 AKShare 获取交易日历失败: %s", exc)
        return None


def is_trading_day(check_date: datetime) -> bool:
    cn_date = check_date.date()
    if cn_date.weekday() >= 5:
        return False
    if LOCAL_CALENDAR_COVERAGE_START <= cn_date <= LOCAL_CALENDAR_COVERAGE_END:
        return not CHINA_CALENDAR.valid_days(start_date=cn_date, end_date=cn_date).empty

    today = datetime.now(SHANGHAI_TZ).date()
    if _trade_day_cache["loaded_on"] != today:
        _trade_day_cache["loaded_on"] = today
        trade_days = _load_trade_days_from_ak()
        if trade_days is not None:
            _trade_day_cache["days"] = trade_days

    trade_days = _trade_day_cache["days"]
    return isinstance(trade_days, set) and cn_date in trade_days


def is_market_hours() -> bool:
    now = datetime.now(SHANGHAI_TZ)
    if not is_trading_day(now):
        return False
    time_now = now.time()
    return (time(9, 30) <= time_now <= time(11, 30)) or (
        time(13, 0) <= time_now <= time(15, 0)
    )
