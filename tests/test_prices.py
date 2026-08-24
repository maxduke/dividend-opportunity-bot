# -*- coding: utf-8 -*-

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src import data_fetcher
from src.opportunity import _parse_datetime

TZ = ZoneInfo("Asia/Shanghai")


def _history(dates, closes, *, basis="qfq", factor=1.0):
    frame = pd.DataFrame({"收盘": closes}, index=pd.to_datetime(dates))
    frame.attrs["price_basis"] = basis
    frame.attrs["adjust_factor"] = factor
    return frame


def _trading_day(monkeypatch):
    monkeypatch.setattr(data_fetcher, "is_trading_day", lambda now: now.weekday() < 5)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-08-24T10:00:00+08:00", datetime(2026, 8, 24, 10, tzinfo=TZ)),
        ("2026-08-24T10:00:00", datetime(2026, 8, 24, 10, tzinfo=TZ)),
        ("2026-08-24T02:00:00+00:00", datetime(2026, 8, 24, 10, tzinfo=TZ)),
        ("not-a-date", None),
        (None, None),
    ],
)
def test_parse_datetime_is_zoneinfo_safe(value, expected):
    assert _parse_datetime(value) == expected


@pytest.mark.parametrize(
    "frame, expected",
    [
        (pd.DataFrame({"close": [10], "day": ["2026-08-24 10:00:00"]}), datetime(2026, 8, 24, 10, tzinfo=TZ)),
        (pd.DataFrame({"close": [10], "datetime": ["2026-08-24T10:00:00+00:00"]}), datetime(2026, 8, 24, 18, tzinfo=TZ)),
        (pd.DataFrame({"close": [10], "日期时间": ["2026-08-24 10:00:00"]}), datetime(2026, 8, 24, 10, tzinfo=TZ)),
        (pd.DataFrame({"close": [10]}, index=["2026-08-24 10:00:00"]), datetime(2026, 8, 24, 10, tzinfo=TZ)),
        (pd.DataFrame({"close": [10], "unknown": ["yesterday"]}), None),
    ],
)
def test_realtime_timestamp_extraction_supports_provider_variants(frame, expected):
    assert data_fetcher._extract_realtime_timestamp(frame, frame.iloc[-1]) == expected


def test_realtime_quote_fetch_preserves_provider_timestamp(monkeypatch):
    frame = pd.DataFrame({"close": [101.5], "day": ["2026-08-24 10:00:00"]})

    async def fake_call(*args, **kwargs):
        return frame

    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)
    quote = asyncio.run(data_fetcher._fetch_single_realtime_quote("510300"))
    assert quote == data_fetcher.RealtimeQuote(101.5, datetime(2026, 8, 24, 10, tzinfo=TZ))


def test_friday_history_saturday_query_does_not_create_bar(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-21"], [100]),
        data_fetcher.RealtimeQuote(101, datetime(2026, 8, 21, 15, tzinfo=TZ)),
        datetime(2026, 8, 22, 10, tzinfo=TZ),
    )
    assert list(result.closes.index.date) == [pd.Timestamp("2026-08-21").date()]
    assert not result.spot_used
    assert result.price_date.isoformat() == "2026-08-21"


def test_monday_preopen_does_not_create_bar_from_stale_quote(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-21"], [100]),
        data_fetcher.RealtimeQuote(101, None),
        datetime(2026, 8, 24, 8, 30, tzinfo=TZ),
    )
    assert len(result.closes) == 1
    assert not result.spot_used


def test_current_day_quote_appends_after_market_open(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-21"], [100]),
        data_fetcher.RealtimeQuote(101, datetime(2026, 8, 24, 10, tzinfo=TZ)),
        datetime(2026, 8, 24, 10, 1, tzinfo=TZ),
    )
    assert list(result.closes) == [100, 101]
    assert result.spot_used and result.price_date.isoformat() == "2026-08-24"


def test_current_day_history_is_replaced_without_duplicate(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-24"], [100]),
        data_fetcher.RealtimeQuote(101, datetime(2026, 8, 24, 10, tzinfo=TZ)),
        datetime(2026, 8, 24, 10, 1, tzinfo=TZ),
    )
    assert len(result.closes) == 1
    assert result.closes.iloc[0] == 101


def test_stale_previous_day_quote_is_not_treated_as_today(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-21"], [100]),
        data_fetcher.RealtimeQuote(101, datetime(2026, 8, 21, 15, tzinfo=TZ)),
        datetime(2026, 8, 24, 10, tzinfo=TZ),
    )
    assert len(result.closes) == 1
    assert result.closes.iloc[0] == 100
    assert not result.spot_used


def test_timestamp_less_quote_is_allowed_after_market_open(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-21"], [100]),
        data_fetcher.RealtimeQuote(101, None),
        datetime(2026, 8, 24, 9, 30, tzinfo=TZ),
    )
    assert result.spot_used
    assert result.closes.iloc[-1] == 101


def test_missing_adjustment_factor_keeps_confirmed_qfq_close(monkeypatch):
    _trading_day(monkeypatch)
    result = data_fetcher.build_indicator_close_series(
        _history(["2026-08-21"], [100], factor=None),
        data_fetcher.RealtimeQuote(101, datetime(2026, 8, 24, 10, tzinfo=TZ)),
        datetime(2026, 8, 24, 10, tzinfo=TZ),
    )
    assert list(result.closes) == [100]
    assert not result.spot_used
    assert "adjustment factor unavailable" in result.note


def test_unadjusted_fallback_disables_all_technical_scores(monkeypatch):
    from src.opportunity import evaluate_opportunity

    history = _history(pd.date_range("2025-01-01", periods=300), [100] * 300, basis="unadjusted_fallback")
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation", AsyncMock(return_value=None)
    )
    snapshot = asyncio.run(
        evaluate_opportunity(
            {"id": 1, "asset_code": "510300", "asset_name": "ETF", "benchmark_code": "000922", "benchmark_name": "红利", "min_score": 60},
            SimpleNamespace(bot_data={}),
            spot_price=101,
            hist_df=history,
        )
    )
    assert snapshot.ma200 is None and snapshot.high_52w is None and snapshot.rsi6 is None
    assert snapshot.long_term_score == snapshot.tactical_score == 0
