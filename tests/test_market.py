from datetime import datetime
from zoneinfo import ZoneInfo

from src.market import is_trading_day


def test_local_xshg_calendar_distinguishes_holiday_and_trading_day():
    tz = ZoneInfo("Asia/Shanghai")
    assert not is_trading_day(datetime(2024, 10, 1, tzinfo=tz))
    assert is_trading_day(datetime(2024, 10, 8, tzinfo=tz))
