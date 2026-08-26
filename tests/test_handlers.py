import asyncio
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest


def test_refresh_clears_history_cache_failure_state_and_date():
    from src import handlers
    from src.config import KEY_CACHE_DATE, KEY_HIST_CACHE, KEY_HIST_FAILURE_CACHE

    context = SimpleNamespace(
        bot_data={
            KEY_HIST_CACHE: {"510300": object()},
            KEY_HIST_FAILURE_CACHE: {"510300": object()},
            KEY_CACHE_DATE: "2026-08-24",
        }
    )
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    asyncio.run(handlers.refresh_cache_command.__wrapped__(update, context))

    assert context.bot_data[KEY_HIST_CACHE] == {}
    assert context.bot_data[KEY_HIST_FAILURE_CACHE] == {}
    assert context.bot_data[KEY_CACHE_DATE] is None


def test_addop_rejects_unsupported_asset_before_network(monkeypatch):
    from src import handlers

    reply = AsyncMock()
    quote = AsyncMock(side_effect=AssertionError("unsupported asset must stop first"))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9),
        message=SimpleNamespace(reply_text=reply),
    )
    context = SimpleNamespace(args=["900001", "000922", "60"], bot_data={})
    monkeypatch.setattr(handlers, "_fetch_single_realtime_quote", quote)

    asyncio.run(handlers.add_opportunity_rule_command.__wrapped__(update, context))

    assert "不支持资产代码 900001" in reply.await_args.args[0]
    quote.assert_not_awaited()


def test_proxy_status_refresh_requires_restart_without_hot_install(monkeypatch):
    from src import handlers
    from src.proxy_health import LOW_BALANCE, POSITIVE, ProxyBalanceStatus

    reply = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    context = SimpleNamespace(
        args=["refresh"],
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    status = ProxyBalanceStatus(
        POSITIVE,
        382.0,
        datetime(2026, 8, 24, 20, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr(handlers, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(handlers, "check_proxy_balance_async", AsyncMock(return_value=status))
    monkeypatch.setattr(handlers, "notify_proxy_health", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers, "proxy_health_category", lambda _: LOW_BALANCE)
    monkeypatch.setattr(handlers, "proxy_patch_active", lambda: False)

    asyncio.run(handlers.proxy_status_command.__wrapped__(update, context))

    handlers.check_proxy_balance_async.assert_awaited_once_with(force=True)
    text = reply.await_args.args[0]
    assert "余额：382" in text
    assert "余额状态：余额偏低" in text
    assert "补丁已启用：否" in text
    assert "请重启 Bot 以安全启用。" in text
    assert "secret-token" not in text


def test_briefing_write_propagates_database_failure(monkeypatch):
    from src import handlers

    def fail(*args, **kwargs):
        assert kwargs["swallow_errors"] is False
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(handlers, "db_execute", fail)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(args=["on"])

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        asyncio.run(handlers.briefing_command.__wrapped__(update, context))
    update.message.reply_text.assert_not_awaited()


def test_addop_initial_snapshot_is_critical(monkeypatch):
    from src import handlers

    reply = AsyncMock()
    sent_message = SimpleNamespace(edit_text=AsyncMock())
    reply.return_value = sent_message
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9),
        message=SimpleNamespace(reply_text=reply),
    )
    context = SimpleNamespace(args=["510300", "000922"], bot_data={})
    db_results = iter([None, {"id": 7}])
    snapshot = SimpleNamespace(total_score=72, level="STRONG")
    save = Mock()

    def fake_db_execute(query, *args, **kwargs):
        if query.lstrip().startswith("SELECT"):
            return next(db_results)
        return None

    monkeypatch.setattr(handlers, "db_execute", fake_db_execute)
    monkeypatch.setattr(
        handlers,
        "_fetch_single_realtime_quote",
        AsyncMock(return_value=SimpleNamespace(price=100)),
    )
    monkeypatch.setattr(
        handlers,
        "get_cached_valuation",
        AsyncMock(
            return_value={"dividend_yield2": 5, "benchmark_name": "中证红利"}
        ),
    )
    monkeypatch.setattr(handlers, "backfill_cn10y", AsyncMock())
    monkeypatch.setattr(handlers, "get_asset_name_with_cache", AsyncMock(return_value="红利ETF"))
    monkeypatch.setattr(handlers, "evaluate_opportunity", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(handlers, "save_opportunity_snapshot", save)
    monkeypatch.setattr(handlers, "record_rule_evaluation", Mock())

    asyncio.run(handlers.add_opportunity_rule_command.__wrapped__(update, context))

    save.assert_called_once_with(snapshot, critical=True)
