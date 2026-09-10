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

    asyncio.run(handlers._add_opportunity_rule(update, context, tuple(context.args)))

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

    asyncio.run(handlers._proxy_status_command(update, context, tuple(context.args)))

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
    context = SimpleNamespace(
        args=["510300", "000922"],
        bot_data={
            "quote_failure_counts": {"510300": 2},
            "quote_failure_notification_sent": {"510300": True},
        },
    )
    db_results = iter([None, {"daily_briefing_enabled": 0}])
    snapshot = SimpleNamespace(total_score=72, level="STRONG")
    save = Mock()

    def fake_db_execute(query, *args, **kwargs):
        if query.lstrip().startswith("SELECT"):
            return next(db_results)
        return 7 if kwargs.get("return_lastrowid") else None

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
    monkeypatch.setattr(handlers, "is_whitelisted", lambda uid: True)

    asyncio.run(handlers._add_opportunity_rule(update, context, tuple(context.args)))

    save.assert_called_once_with(snapshot, critical=True)
    assert "监控已创建" in sent_message.edit_text.await_args.args[0]
    assert "盘中自动告警" in sent_message.edit_text.await_args.args[0]
    assert context.bot_data["quote_failure_counts"] == {}
    assert context.bot_data["quote_failure_notification_sent"] == {}


def test_opon_group_suffix_is_idempotent(monkeypatch):
    from src import handlers

    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9),
        message=SimpleNamespace(reply_text=reply, text="/opon@dividend_bot 7"),
    )
    context = SimpleNamespace(args=["7"], bot_data={})
    db = Mock(return_value={"id": 7, "is_active": 1})
    monkeypatch.setattr(handlers, "db_execute", db)

    asyncio.run(handlers.toggle_opportunity_rule_command.__wrapped__(update, context))

    db.assert_called_once()
    assert "已开启" in reply.await_args.args[0]


def test_opon_evaluates_and_stores_immediate_baseline(monkeypatch):
    from src import handlers

    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=9),
        message=SimpleNamespace(reply_text=reply, text="/opon 7"),
    )
    context = SimpleNamespace(args=["7"], bot_data={})
    rule = {"id": 7, "is_active": 0}
    snapshot = SimpleNamespace(total_score=73, level="STRONG")
    db = Mock(return_value=rule)
    evaluate = AsyncMock(return_value=snapshot)
    save = Mock()
    monkeypatch.setattr(handlers, "db_execute", db)
    monkeypatch.setattr(handlers, "evaluate_opportunity", evaluate)
    monkeypatch.setattr(handlers, "save_opportunity_snapshot", save)

    monkeypatch.setattr(handlers, "rule_is_current", lambda rule: True)
    asyncio.run(handlers.set_rule_active(rule, 9, context, True))

    evaluate.assert_awaited_once_with(rule, context)
    save.assert_called_once_with(snapshot, critical=True)
    assert db.call_count == 1
    assert "SET is_active = 1" in db.call_args_list[0].args[0]
    assert "last_score = NULL" not in db.call_args_list[0].args[0]
    assert db.call_args_list[0].args[1][:2] == (73, "STRONG")


@pytest.mark.parametrize('intraday,subscribed,times,manual,button', [
    (False, False, '14:50', True, True),
    (True, False, '14:50', False, True),
    (False, True, '14:50', False, False),
    (False, True, '', True, False),
    (False, False, 'invalid,25:00', True, False),
])
def test_delivery_guidance_reflects_actual_push_configuration(monkeypatch, intraday, subscribed, times, manual, button):
    from src import handlers
    monkeypatch.setattr(handlers, 'ENABLE_INTRADAY_MONITOR', intraday)
    monkeypatch.setattr(handlers, 'BRIEFING_TIMES_STR', times)
    monkeypatch.setattr(handlers, 'db_execute', Mock(return_value={'daily_briefing_enabled':subscribed}))
    text, markup = handlers._delivery_guidance(9)
    assert ('当前仅支持手动查询' in text) is manual
    assert (markup is not None) is button
    if button:
        assert markup.inline_keyboard[0][0].callback_data == 'briefing_on:9'


@pytest.mark.parametrize('owner,whitelisted,configured,allowed', [
    (9, True, True, True), (8, True, True, False),
    (9, False, True, False), (9, True, False, False),
])
def test_briefing_button_checks_owner_and_permissions(monkeypatch, owner, whitelisted, configured, allowed):
    from src import handlers
    query = SimpleNamespace(data=f'briefing_on:{owner}', answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=9), callback_query=query)
    monkeypatch.setattr(handlers, 'is_whitelisted', lambda uid: whitelisted)
    monkeypatch.setattr(handlers, 'BRIEFING_TIMES_STR', '14:50' if configured else '')
    db = Mock(return_value={'daily_briefing_enabled':1})
    monkeypatch.setattr(handlers, 'db_execute', db)
    asyncio.run(handlers.enable_briefing_callback(update, SimpleNamespace()))
    writes = [call for call in db.call_args_list if call.args[0].startswith('UPDATE')]
    assert len(writes) == int(allowed)
    if allowed:
        assert writes[0].args[1] == (9,)
        assert '已开启' in query.message.reply_text.await_args.args[0]
    else:
        query.message.reply_text.assert_not_awaited()
