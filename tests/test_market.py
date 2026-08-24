from datetime import date, datetime, timedelta
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


def test_calendar_provider_failure_retries_after_short_cooldown(monkeypatch):
    check_date = datetime(2026, 8, 24, tzinfo=ZoneInfo("Asia/Shanghai"))
    responses = iter((None, {check_date.date()}))
    cache = {"days": None, "loaded_on": None, "failed_at": None}

    monkeypatch.setattr(market, "_load_trade_days_from_ak", lambda: next(responses))
    monkeypatch.setattr(market, "_trade_day_cache", cache)
    monkeypatch.setattr(
        market, "LOCAL_CALENDAR_COVERAGE_END", check_date.date().replace(year=2025)
    )

    assert not market.is_trading_day(check_date)
    cache["failed_at"] -= market.CALENDAR_FAILURE_RETRY
    assert market.is_trading_day(check_date)


def test_trading_sessions_elapsed_skips_national_day_holiday():
    assert market.trading_sessions_elapsed(date(2024, 9, 30), date(2024, 10, 8)) == 1


def test_trading_sessions_elapsed_counts_four_sessions_after_holiday():
    assert market.trading_sessions_elapsed(date(2024, 10, 8), date(2024, 10, 14)) == 4


def test_trading_sessions_elapsed_returns_none_when_provider_calendar_unavailable(monkeypatch):
    target = date(2026, 8, 24)
    monkeypatch.setattr(market, "_load_trade_days_from_ak", lambda: None)
    monkeypatch.setattr(
        market,
        "_trade_day_cache",
        {"days": None, "loaded_on": None, "failed_at": None},
    )
    monkeypatch.setattr(market, "LOCAL_CALENDAR_COVERAGE_END", date(2025, 12, 31))
    assert market.trading_sessions_elapsed(target - timedelta(days=1), target) is None
