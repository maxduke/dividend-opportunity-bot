import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo


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
