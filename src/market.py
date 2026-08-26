# -*- coding: utf-8 -*-

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import pandas_market_calendars as mcal

from .config import AKSHARE_CALL_TIMEOUT_SECONDS

CHINA_CALENDAR = mcal.get_calendar("XSHG")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

_LOCAL_HOLIDAYS = CHINA_CALENDAR.adhoc_holidays
LOCAL_CALENDAR_COVERAGE_START = _LOCAL_HOLIDAYS.min().date()
LOCAL_CALENDAR_COVERAGE_END = _LOCAL_HOLIDAYS.max().date()
CALENDAR_FAILURE_RETRY = timedelta(minutes=1)
_trade_day_cache = {"days": None, "loaded_on": None, "failed_at": None}
_trade_day_refresh_lock = asyncio.Lock()
_trade_day_refresh_task = None


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


def _calendar_refresh_due(now: datetime) -> bool:
    failed_at = _trade_day_cache.get("failed_at")
    return (
        _trade_day_cache.get("loaded_on") != now.date()
        and (failed_at is None or now - failed_at >= CALENDAR_FAILURE_RETRY)
    )


async def ensure_trade_days_loaded(check_date: datetime | None = None) -> None:
    """Refresh the provider calendar without blocking the event loop."""
    global _trade_day_refresh_task

    target = check_date or datetime.now(SHANGHAI_TZ)
    cn_date = target.astimezone(SHANGHAI_TZ).date() if target.tzinfo else target.date()
    if LOCAL_CALENDAR_COVERAGE_START <= cn_date <= LOCAL_CALENDAR_COVERAGE_END:
        return

    now = datetime.now(SHANGHAI_TZ)
    if not _calendar_refresh_due(now):
        return
    async with _trade_day_refresh_lock:
        now = datetime.now(SHANGHAI_TZ)
        if not _calendar_refresh_due(now):
            return
        if _trade_day_refresh_task is None:
            _trade_day_refresh_task = asyncio.create_task(
                asyncio.to_thread(_load_trade_days_from_ak)
            )
        try:
            trade_days = await asyncio.wait_for(
                asyncio.shield(_trade_day_refresh_task),
                timeout=AKSHARE_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("交易日历加载超时(%ss)", AKSHARE_CALL_TIMEOUT_SECONDS)
            trade_days = None
        except Exception as exc:
            logger.warning("异步加载交易日历失败: %s", exc)
            trade_days = None
        if _trade_day_refresh_task.done():
            _trade_day_refresh_task = None
        if trade_days is None:
            _trade_day_cache["failed_at"] = now
        else:
            _trade_day_cache["days"] = trade_days
            _trade_day_cache["loaded_on"] = now.date()
            _trade_day_cache["failed_at"] = None


def is_trading_day(check_date: datetime) -> bool:
    cn_date = check_date.date()
    if cn_date.weekday() >= 5:
        return False
    if LOCAL_CALENDAR_COVERAGE_START <= cn_date <= LOCAL_CALENDAR_COVERAGE_END:
        return not CHINA_CALENDAR.valid_days(start_date=cn_date, end_date=cn_date).empty

    now = datetime.now(SHANGHAI_TZ)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_async_context = False
    else:
        running_async_context = True
    if not running_async_context and _calendar_refresh_due(now):
        trade_days = _load_trade_days_from_ak()
        if trade_days is None:
            _trade_day_cache["failed_at"] = now
        else:
            _trade_day_cache["days"] = trade_days
            _trade_day_cache["loaded_on"] = now.date()
            _trade_day_cache["failed_at"] = None

    trade_days = _trade_day_cache["days"]
    return isinstance(trade_days, set) and cn_date in trade_days


def trading_sessions_elapsed(start_date: date, end_date: date) -> int | None:
    """Count XSHG sessions after ``start_date`` through ``end_date``.

    ``None`` means that the calendar could not be established for the whole
    interval.  The existing local XSHG calendar and trade-day provider cache
    are deliberately reused; no additional provider/retry loop is introduced.
    """
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    if start_date > end_date:
        return None

    sessions = 0
    cursor = start_date + timedelta(days=1)
    while cursor <= end_date:
        # Weekends are known non-sessions without consulting a provider.
        if cursor.weekday() < 5:
            is_session = is_trading_day(
                datetime.combine(cursor, time.min, tzinfo=SHANGHAI_TZ)
            )
            if not (
                LOCAL_CALENDAR_COVERAGE_START <= cursor <= LOCAL_CALENDAR_COVERAGE_END
                or isinstance(_trade_day_cache.get("days"), set)
            ):
                return None
            if is_session:
                sessions += 1
        cursor += timedelta(days=1)
    return sessions


def is_market_hours() -> bool:
    now = datetime.now(SHANGHAI_TZ)
    if not is_trading_day(now):
        return False
    time_now = now.time()
    return (time(9, 30) <= time_now <= time(11, 30)) or (
        time(13, 0) <= time_now <= time(15, 0)
    )
