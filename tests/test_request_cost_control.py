import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd


def _enable_paid_proxy(monkeypatch, data_fetcher):
    """Keep request-cost tests focused on routing, not global health state."""
    monkeypatch.setattr(data_fetcher, "check_proxy_balance_async", AsyncMock(
        return_value=SimpleNamespace(state="POSITIVE")
    ))
    monkeypatch.setattr(data_fetcher, "proxy_patch_active", lambda: True)


def test_proxy_disables_application_level_eastmoney_retries(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    assert data_fetcher._em_retry_attempts() == 1


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
    _enable_paid_proxy(monkeypatch, data_fetcher)
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
    _enable_paid_proxy(monkeypatch, data_fetcher)
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("510300", 550))

    assert result is not None
    assert calls.count("fund_etf_hist_em") == 1
    assert calls.count("fund_etf_hist_sina") == 0
    assert result.attrs["price_basis"] == "qfq"
    assert result.attrs["price_basis_asof"] == datetime.now(
        data_fetcher.SHANGHAI_TZ
    ).date().isoformat()


def test_history_price_adjust_is_forwarded_and_tagged(monkeypatch):
    from src import data_fetcher

    calls = []
    history = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=300),
            "收盘": [100.0] * 300,
        }
    )

    async def fake_call(function, *args, **kwargs):
        calls.append((function.__name__, kwargs))
        return history.copy()

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    _enable_paid_proxy(monkeypatch, data_fetcher)
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("510300", 550, price_adjust="hfq"))

    assert result.attrs["price_basis"] == "hfq"
    assert calls == [("fund_etf_hist_em", {
        "symbol": "510300",
        "period": "daily",
        "start_date": calls[0][1]["start_date"],
        "end_date": calls[0][1]["end_date"],
        "adjust": "hfq",
        "timeout_seconds": data_fetcher.AKSHARE_PROXY_CALL_TIMEOUT_SECONDS,
    })]


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
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("510300", 550))

    assert result.attrs["price_basis"] == "unadjusted_fallback"
    assert result.attrs["price_basis_asof"] == datetime.now(
        data_fetcher.SHANGHAI_TZ
    ).date().isoformat()


def test_runtime_zero_balance_skips_paid_etf_request(monkeypatch):
    from src import data_fetcher

    calls = []
    raw = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=300),
            "收盘": [100.0] * 300,
        }
    )

    async def fake_call(function, *args, **kwargs):
        calls.append(function.__name__)
        return raw.copy()

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(
        data_fetcher,
        "check_proxy_balance_async",
        AsyncMock(return_value=SimpleNamespace(state="NO_BALANCE_OR_INVALID")),
    )
    monkeypatch.setattr(data_fetcher, "proxy_patch_active", lambda: True)
    monkeypatch.setattr(data_fetcher, "_call_akshare", fake_call)

    result = asyncio.run(data_fetcher.get_history_data("510300", 550))

    assert "fund_etf_hist_em" not in calls
    assert calls == ["fund_etf_hist_sina"]
    assert result.attrs["price_basis"] == "unadjusted_fallback"


def _history_frame(*, basis, asof, rows=369):
    frame = pd.DataFrame(
        {
            "收盘": [100.0] * rows,
        },
        index=pd.date_range("2025-01-01", periods=rows),
    )
    frame.attrs.update(
        technical_history_days=550,
        price_basis=basis,
        price_basis_asof=asof,
    )
    return frame


def test_runtime_history_requires_current_qfq_basis():
    from src import data_fetcher

    now = datetime.fromisoformat("2026-08-24T10:00:00+08:00")
    raw = _history_frame(basis="unadjusted_fallback", asof="2026-08-24")
    stale_qfq = _history_frame(basis="qfq", asof="2026-08-23")
    current_qfq = _history_frame(basis="qfq", asof="2026-08-24")

    assert not data_fetcher.runtime_history_is_usable(raw, 550, now)
    assert not data_fetcher.runtime_history_is_usable(stale_qfq, 550, now)
    assert data_fetcher.runtime_history_is_usable(current_qfq, 550, now)


def test_raw_fallback_is_retained_but_marks_history_failure(monkeypatch):
    from src import data_fetcher

    now = datetime.fromisoformat("2026-08-24T10:00:00+08:00")
    raw = _history_frame(basis="unadjusted_fallback", asof="2026-08-24")
    fetch = AsyncMock(return_value=raw)
    monkeypatch.setattr(data_fetcher, "get_history_data", fetch)
    context = SimpleNamespace(bot_data={})

    result = asyncio.run(data_fetcher.get_history_data_cached(context, "510300", 550, now))

    assert result is raw
    assert context.bot_data[data_fetcher.KEY_HIST_CACHE]["510300"] is raw
    assert context.bot_data[data_fetcher.KEY_HIST_FAILURE_CACHE]["510300"] == now


def test_raw_fallback_cooldown_skips_provider_call(monkeypatch):
    from src import data_fetcher

    now = datetime.fromisoformat("2026-08-24T10:00:00+08:00")
    raw = _history_frame(basis="unadjusted_fallback", asof="2026-08-24")
    fetch = AsyncMock(return_value=raw)
    monkeypatch.setattr(data_fetcher, "get_history_data", fetch)
    monkeypatch.setattr(data_fetcher, "HISTORY_FAILURE_COOLDOWN_MINUTES", 30)
    context = SimpleNamespace(bot_data={})

    asyncio.run(data_fetcher.get_history_data_cached(context, "510300", 550, now))
    result = asyncio.run(
        data_fetcher.get_history_data_cached(
            context, "510300", 550, now + timedelta(minutes=5)
        )
    )

    assert result is raw
    fetch.assert_awaited_once()


def test_history_cache_single_flight_for_same_asset(monkeypatch):
    from src import data_fetcher

    now = datetime.fromisoformat("2026-08-24T10:00:00+08:00")
    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    history.attrs.update(
        technical_history_days=550,
        price_basis="qfq",
        price_basis_asof="2026-08-24",
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return history

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(data_fetcher, "get_history_data", fetch_mock)
    context = SimpleNamespace(bot_data={})

    async def exercise():
        first = asyncio.create_task(
            data_fetcher.get_history_data_cached(context, "510300", 550, now)
        )
        await entered.wait()
        second = asyncio.create_task(
            data_fetcher.get_history_data_cached(context, "510300", 550, now)
        )
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(exercise())
    assert first is history and second is history
    fetch_mock.assert_awaited_once()


def test_realtime_quote_failures_are_tracked_per_asset_and_notified_once(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(data_fetcher, "FETCH_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(data_fetcher, "ADMIN_USER_ID", 12345)
    quote = data_fetcher.RealtimeQuote(1.0)
    fetch = AsyncMock(side_effect=[None, None, None, quote, None, quote, None, None])
    monkeypatch.setattr(data_fetcher, "_fetch_single_realtime_quote", fetch)
    send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None, None])
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message), bot_data={})

    async def exercise():
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300", "000922"])
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300", "000922"])
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300"])
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300"])
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300"])
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300"])

    asyncio.run(exercise())

    assert send_message.await_count == 3
    assert "510300" in send_message.await_args.kwargs["text"]
    assert context.bot_data[data_fetcher.KEY_QUOTE_FAILURE_COUNTS] == {"510300": 2}
    assert context.bot_data[data_fetcher.KEY_QUOTE_FAILURE_NOTIFIED] == {"510300": True}


def test_realtime_quote_failure_alert_is_split_before_sending(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(data_fetcher, "FETCH_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(data_fetcher, "ADMIN_USER_ID", 12345)
    monkeypatch.setattr(
        data_fetcher, "_fetch_single_realtime_quote", AsyncMock(return_value=None)
    )
    send_message = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message), bot_data={})
    codes = [f"{code:06d}" for code in range(200)]

    asyncio.run(data_fetcher._fetch_all_realtime_quotes(context, codes))

    assert send_message.await_count > 1
    assert all(
        len(call.kwargs["text"]) <= 3800 for call in send_message.await_args_list
    )


def test_recovered_quote_is_not_remarked_notified_after_alert_send(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(data_fetcher, "FETCH_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(data_fetcher, "ADMIN_USER_ID", 12345)
    monkeypatch.setattr(
        data_fetcher, "_fetch_single_realtime_quote", AsyncMock(return_value=None)
    )
    context = SimpleNamespace(bot_data={})

    async def recover_during_send(*args, **kwargs):
        if send_message.await_count == 1:
            context.bot_data[data_fetcher.KEY_QUOTE_FAILURE_COUNTS].pop("510300")
            context.bot_data[data_fetcher.KEY_QUOTE_FAILURE_NOTIFIED].pop("510300", None)

    send_message = AsyncMock(side_effect=recover_during_send)
    context.bot = SimpleNamespace(send_message=send_message)

    async def exercise():
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300"])
        assert "510300" not in context.bot_data[data_fetcher.KEY_QUOTE_FAILURE_NOTIFIED]
        await data_fetcher._fetch_all_realtime_quotes(context, ["510300"])

    asyncio.run(exercise())

    assert send_message.await_count == 2
    assert context.bot_data[data_fetcher.KEY_QUOTE_FAILURE_NOTIFIED] == {"510300": True}


def test_qfq_retry_replaces_raw_fallback_and_clears_failure(monkeypatch):
    from src import data_fetcher

    now = datetime.fromisoformat("2026-08-24T10:00:00+08:00")
    raw = _history_frame(basis="unadjusted_fallback", asof="2026-08-24")
    qfq = _history_frame(basis="qfq", asof="2026-08-24")
    fetch = AsyncMock(side_effect=[raw, qfq])
    monkeypatch.setattr(data_fetcher, "get_history_data", fetch)
    monkeypatch.setattr(data_fetcher, "HISTORY_FAILURE_COOLDOWN_MINUTES", 30)
    context = SimpleNamespace(bot_data={})

    asyncio.run(data_fetcher.get_history_data_cached(context, "510300", 550, now))
    result = asyncio.run(
        data_fetcher.get_history_data_cached(
            context, "510300", 550, now + timedelta(minutes=31)
        )
    )

    assert result is qfq
    assert context.bot_data[data_fetcher.KEY_HIST_CACHE]["510300"] is qfq
    assert "510300" not in context.bot_data[data_fetcher.KEY_HIST_FAILURE_CACHE]
    assert fetch.await_count == 2


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

    assert name == "资产_510300"


def test_unusable_proxy_does_not_call_stock_name_endpoint(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(data_fetcher, "REQUEST_INTERVAL_SECONDS", 0)
    active = False
    monkeypatch.setattr(data_fetcher, "proxy_patch_active", lambda: active)
    balance_check = AsyncMock(
        return_value=SimpleNamespace(state="NO_BALANCE_OR_INVALID")
    )
    monkeypatch.setattr(data_fetcher, "check_proxy_balance_async", balance_check)
    monkeypatch.setattr(
        data_fetcher,
        "_call_akshare",
        AsyncMock(side_effect=AssertionError("stock name endpoint must not be called")),
    )

    name = asyncio.run(
        data_fetcher.get_asset_name_with_cache(
            "600000", SimpleNamespace(bot_data={})
        )
    )

    assert name == "资产_600000"
    balance_check.assert_not_awaited()

    active = True
    name = asyncio.run(
        data_fetcher.get_asset_name_with_cache(
            "600001", SimpleNamespace(bot_data={})
        )
    )

    assert name == "资产_600001"
    balance_check.assert_awaited_once()


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


def test_failed_valuation_refresh_is_not_repeated_during_cooldown(monkeypatch):
    from src import valuation_fetcher

    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(valuation_fetcher, "get_latest_valuation", lambda code: None)
    monkeypatch.setattr(valuation_fetcher, "fetch_csi_valuation", fetch)
    bot_data = {}

    assert asyncio.run(valuation_fetcher.get_cached_valuation("000922", bot_data)) is None
    assert asyncio.run(valuation_fetcher.get_cached_valuation("000922", bot_data)) is None
    fetch.assert_awaited_once()


def test_failed_valuation_refresh_retries_after_failure_cooldown(monkeypatch):
    from src import valuation_fetcher

    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(valuation_fetcher, "get_latest_valuation", lambda code: None)
    monkeypatch.setattr(valuation_fetcher, "fetch_csi_valuation", fetch)
    first_now = datetime(2026, 8, 24, 10, 0, tzinfo=valuation_fetcher.SHANGHAI_TZ)
    monkeypatch.setattr(valuation_fetcher, "_now", lambda: first_now)
    bot_data = {}

    assert asyncio.run(valuation_fetcher.get_cached_valuation("000922", bot_data)) is None
    monkeypatch.setattr(
        valuation_fetcher,
        "_now",
        lambda: first_now + valuation_fetcher.PROVIDER_FAILURE_COOLDOWN + timedelta(seconds=1),
    )
    assert asyncio.run(valuation_fetcher.get_cached_valuation("000922", bot_data)) is None
    assert fetch.await_count == 2


def test_fresh_persisted_bond_skips_network(monkeypatch):
    from src import valuation_fetcher

    latest = {
        "yield_date": datetime.now(valuation_fetcher.SHANGHAI_TZ).date().isoformat(),
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


def test_failed_bond_refresh_retries_after_failure_cooldown(monkeypatch):
    from src import valuation_fetcher

    fetch = AsyncMock(return_value=(None, "sina"))
    monkeypatch.setattr(valuation_fetcher, "latest_bond_on_or_before", lambda *args, **kwargs: None)
    monkeypatch.setattr(valuation_fetcher, "fetch_cn10y", fetch)
    first_now = datetime(2026, 8, 24, 10, 0, tzinfo=valuation_fetcher.SHANGHAI_TZ)
    monkeypatch.setattr(valuation_fetcher, "_now", lambda: first_now)
    bot_data = {}

    assert asyncio.run(valuation_fetcher.get_cached_cn10y(bot_data)) is None
    assert asyncio.run(valuation_fetcher.get_cached_cn10y(bot_data)) is None
    assert fetch.await_count == 1

    monkeypatch.setattr(
        valuation_fetcher,
        "_now",
        lambda: first_now + valuation_fetcher.PROVIDER_FAILURE_COOLDOWN + timedelta(seconds=1),
    )
    assert asyncio.run(valuation_fetcher.get_cached_cn10y(bot_data)) is None
    assert fetch.await_count == 2
