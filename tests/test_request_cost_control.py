import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd


def test_proxy_history_prefers_sina_and_skips_eastmoney(monkeypatch):
    from src import data_fetcher

    calls = []
    history = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=300),
            "收盘": [100.0] * 300,
        }
    )

    async def fake_call(function, *args, **kwargs):
        calls.append(function.__name__)
        return history.copy()

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(data_fetcher, "USE_ADJUST", True)
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("000001", 550))

    assert result is not None
    assert "stock_zh_a_daily" in calls
    assert "stock_zh_a_hist" not in calls


def test_adjusted_etf_history_uses_one_eastmoney_call(monkeypatch):
    from src import data_fetcher

    calls = []
    history = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=300),
            "收盘": [100.0] * 300,
        }
    )

    async def fake_call(function, *args, **kwargs):
        calls.append(function.__name__)
        return history.copy()

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(data_fetcher, "USE_ADJUST", True)
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("510300", 550))

    assert result is not None
    assert calls.count("fund_etf_hist_em") == 1
    # Sina is still used for the free raw-price adjustment-factor lookup.
    assert calls.count("fund_etf_hist_sina") == 1


def test_adjusted_etf_sina_fallback_is_marked_unadjusted(monkeypatch):
    from src import data_fetcher

    history = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=300),
            "收盘": [100.0] * 300,
        }
    )

    async def fake_call(function, *args, **kwargs):
        if function.__name__ == "fund_etf_hist_em":
            return None
        return history.copy()

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(data_fetcher, "USE_ADJUST", True)
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("510300", 550))

    assert result.attrs["price_basis"] == "unadjusted_fallback"


def test_failed_history_is_not_retried_during_cooldown(monkeypatch):
    from src import data_fetcher

    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(data_fetcher, "get_history_data", fetch)
    monkeypatch.setattr(data_fetcher, "HISTORY_FAILURE_COOLDOWN_MINUTES", 30)
    context = SimpleNamespace(bot_data={})
    now = datetime.fromisoformat("2026-08-13T10:00:00+08:00")

    first = asyncio.run(data_fetcher.get_history_data_cached(context, "510300", 550, now))
    second = asyncio.run(
        data_fetcher.get_history_data_cached(
            context,
            "510300",
            550,
            datetime.fromisoformat("2026-08-13T10:05:00+08:00"),
        )
    )

    assert first is None and second is None
    fetch.assert_awaited_once()


def test_proxy_mode_does_not_call_unproxyable_etf_name_endpoint(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(data_fetcher, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        data_fetcher,
        "_call_akshare",
        AsyncMock(side_effect=AssertionError("fund_name_em must not be called")),
    )
    context = SimpleNamespace(bot_data={})

    name = asyncio.run(data_fetcher.get_asset_name_with_cache("510300", context))

    assert name == "Asset_510300"


def test_fresh_persisted_valuation_skips_network(monkeypatch):
    from src import valuation_fetcher

    latest = {
        "benchmark_code": "000922",
        "valuation_date": "2026-08-13",
        "fetched_at": datetime.now(valuation_fetcher.SHANGHAI_TZ).isoformat(),
        "dividend_yield1": 5.0,
        "dividend_yield2": 5.2,
    }
    fetch = AsyncMock(side_effect=AssertionError("network fetch should be skipped"))
    monkeypatch.setattr(valuation_fetcher, "get_latest_valuation", lambda code: latest)
    monkeypatch.setattr(valuation_fetcher, "fetch_csi_valuation", fetch)

    result = asyncio.run(valuation_fetcher.get_cached_valuation("000922", {}))

    assert result == latest
    fetch.assert_not_awaited()


def test_failed_valuation_refresh_is_not_repeated_within_ttl(monkeypatch):
    from src import valuation_fetcher

    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(valuation_fetcher, "get_latest_valuation", lambda code: None)
    monkeypatch.setattr(valuation_fetcher, "fetch_csi_valuation", fetch)
    bot_data = {}

    assert asyncio.run(valuation_fetcher.get_cached_valuation("000922", bot_data)) is None
    assert asyncio.run(valuation_fetcher.get_cached_valuation("000922", bot_data)) is None
    fetch.assert_awaited_once()


def test_fresh_persisted_bond_skips_network(monkeypatch):
    from src import valuation_fetcher

    latest = {
        "yield_date": date.today().isoformat(),
        "fetched_at": datetime.now(valuation_fetcher.SHANGHAI_TZ).isoformat(),
        "cn10y": 1.82,
        "source": "chinabond",
    }
    fetch = AsyncMock(side_effect=AssertionError("network fetch should be skipped"))
    monkeypatch.setattr(
        valuation_fetcher,
        "latest_bond_on_or_before",
        lambda target_date, max_gap_days: latest,
    )
    monkeypatch.setattr(valuation_fetcher, "fetch_cn10y", fetch)

    result = asyncio.run(valuation_fetcher.get_cached_cn10y({}))

    assert result == latest
    fetch.assert_not_awaited()
