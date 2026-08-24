from datetime import datetime
from zoneinfo import ZoneInfo

from src import market


def test_local_xshg_calendar_distinguishes_holiday_and_trading_day():
    tz = ZoneInfo("Asia/Shanghai")
    assert not market.is_trading_day(datetime(2024, 10, 1, tzinfo=tz))
    assert market.is_trading_day(datetime(2024, 10, 8, tzinfo=tz))


def test_dates_beyond_local_calendar_coverage_use_daily_provider_cache(monkeypatch):
    tz = ZoneInfo("Asia/Shanghai")
    check_date = datetime(2026, 8, 24, tzinfo=tz)
    calls = 0

    def load_trade_days():
        nonlocal calls
        calls += 1
        return {check_date.date()}

    monkeypatch.setattr(market, "_load_trade_days_from_ak", load_trade_days)
    monkeypatch.setattr(market, "_trade_day_cache", {"days": None, "loaded_on": None})

    # Force the provider path while keeping the assertion date deterministic.
    monkeypatch.setattr(
        market, "LOCAL_CALENDAR_COVERAGE_END", check_date.date().replace(year=2025)
    )
    assert market.is_trading_day(check_date)
    assert market.is_trading_day(check_date)
    assert calls == 1
