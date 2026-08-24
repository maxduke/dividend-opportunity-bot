# -*- coding: utf-8 -*-

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

CHINA_CALENDAR = mcal.get_calendar('XSHG')
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def is_trading_day(check_date: datetime) -> bool:
    cn_date = check_date.date()
    return not CHINA_CALENDAR.valid_days(start_date=cn_date, end_date=cn_date).empty


def is_market_hours() -> bool:
    now = datetime.now(SHANGHAI_TZ)
    if not is_trading_day(now):
        return False
    time_now = now.time()
    return (time(9, 30) <= time_now <= time(11, 30)) or (
        time(13, 0) <= time_now <= time(15, 0)
    )
