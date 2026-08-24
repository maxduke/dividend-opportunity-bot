import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd


def _raw_history():
    frame = pd.DataFrame(
        {"收盘": [100.0] * 369},
        index=pd.date_range("2025-01-01", periods=369),
    )
    frame.attrs.update(
        technical_history_days=550,
        price_basis="unadjusted_fallback",
        price_basis_asof="2026-08-24",
    )
    return frame


def test_check_job_skips_overlapping_run(monkeypatch):
    from src import jobs

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_run(context):
        entered.set()
        await release.wait()

    runner = AsyncMock(side_effect=blocking_run)
    monkeypatch.setattr(jobs, "_check_opportunity_job", runner)

    async def exercise():
        context = SimpleNamespace(bot_data={})
        first = asyncio.create_task(jobs.check_opportunity_job(context))
        await entered.wait()
        await jobs.check_opportunity_job(context)
        release.set()
        await first

    asyncio.run(exercise())
    runner.assert_awaited_once()


def test_daily_briefing_stops_before_provider_access_without_subscribers(monkeypatch):
    from src import jobs

    monkeypatch.setattr(jobs, "is_trading_day", lambda now: True)
    monkeypatch.setattr(jobs, "db_execute", lambda *args, **kwargs: [])
    provider = AsyncMock(side_effect=AssertionError("provider should not run"))
    monkeypatch.setattr(jobs, "_fetch_all_realtime_quotes", provider)

    asyncio.run(jobs.daily_briefing_job(SimpleNamespace(bot_data={})))
    provider.assert_not_awaited()


def test_opportunity_loader_keeps_degraded_cache_in_cooldown(monkeypatch):
    from src import jobs

    now = datetime.fromisoformat("2026-08-24T10:05:00+08:00")
    raw = _raw_history()
    context = SimpleNamespace(
        bot_data={
            "hist_data_cache": {"510300": raw},
            "hist_failure_cache": {"510300": datetime.fromisoformat("2026-08-24T10:00:00+08:00")},
            "cache_date": "2026-08-24",
        }
    )
    provider = AsyncMock(side_effect=AssertionError("cooldown must skip provider"))
    monkeypatch.setattr(jobs, "get_history_data_cached", provider)
    monkeypatch.setattr(jobs, "history_failure_is_fresh", lambda *args, **kwargs: True)

    history = asyncio.run(jobs._load_opportunity_history(context, ["510300"], now))

    assert history["510300"] is raw
    provider.assert_not_awaited()


def test_production_evaluator_does_not_pass_degraded_history(monkeypatch):
    from src import jobs

    now = datetime.fromisoformat("2026-08-24T10:00:00+08:00")
    calls = []
    snapshot = SimpleNamespace(technical_price_basis="unavailable")
    rule = {"id": 1, "asset_code": "510300", "user_id": 9}

    async def fake_evaluate(*args, **kwargs):
        calls.append(kwargs)
        return snapshot

    monkeypatch.setattr(jobs, "evaluate_opportunity", fake_evaluate)
    monkeypatch.setattr(jobs, "should_send_opportunity_alert", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr(jobs, "snapshot_should_persist", lambda *args, **kwargs: False)
    monkeypatch.setattr(jobs, "record_rule_evaluation", lambda *args, **kwargs: None)
    monkeypatch.setattr(jobs, "_opportunity_alerts_today", lambda *args, **kwargs: 0)

    asyncio.run(
        jobs._evaluate_opportunity_rules(
            SimpleNamespace(bot_data={}),
            [rule],
            {"510300": SimpleNamespace(price=1.453)},
            {"510300": _raw_history()},
            now,
        )
    )

    assert calls and calls[0]["hist_df"] is None


def test_daily_briefing_keeps_degraded_banner_and_rule_details(monkeypatch):
    from src import jobs
    from src.opportunity import OpportunitySnapshot

    rule = {
        "id": 1,
        "user_id": 9,
        "asset_code": "510300",
        "asset_name": "红利ETF",
        "benchmark_code": "000922",
        "benchmark_name": "中证红利",
    }
    snapshot = OpportunitySnapshot(
        rule_id=1,
        asset_code="510300",
        asset_name="红利ETF",
        benchmark_code="000922",
        benchmark_name="中证红利",
        snapshot_at="2026-08-24T10:00:00+08:00",
        total_score=42,
        level="WATCH",
        data_quality="DEGRADED",
        technical_price_basis="unavailable",
    )
    send_message = AsyncMock()
    context = SimpleNamespace(
        bot=SimpleNamespace(send_message=send_message),
        bot_data={},
    )
    monkeypatch.setattr(jobs, "is_trading_day", lambda now: True)
    monkeypatch.setattr(
        jobs,
        "db_execute",
        Mock(side_effect=[[{"user_id": 9}], [rule]]),
    )
    monkeypatch.setattr(
        jobs,
        "_fetch_all_realtime_quotes",
        AsyncMock(return_value=({"510300": SimpleNamespace(price=1.453)}, True)),
    )
    monkeypatch.setattr(jobs, "_load_opportunity_history", AsyncMock(return_value={"510300": _raw_history()}))
    monkeypatch.setattr(jobs, "evaluate_opportunity", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(jobs, "save_opportunity_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(jobs, "record_rule_evaluation", lambda *args, **kwargs: None)

    asyncio.run(jobs.daily_briefing_job(context))

    text = send_message.await_args.kwargs["text"]
    assert "Technical data degraded for one or more assets." in text
    assert "Data: <code>DEGRADED</code>" in text
    assert "Technical: unavailable" in text


def test_intraday_registration_disabled():
    from src.main import _register_intraday_job

    queue = Mock()
    _register_intraday_job(queue, object(), enabled=False)
    queue.run_repeating.assert_not_called()


def test_intraday_registration_enabled_once():
    from src import main

    queue = Mock()
    job = object()
    main._register_intraday_job(queue, job, enabled=True)
    queue.run_repeating.assert_called_once_with(
        job, interval=main.CHECK_INTERVAL_SECONDS, first=10
    )


def test_command_menu_has_only_opportunity_product_surface(monkeypatch):
    from src import main
    from src.config import KEY_HIST_FAILURE_CACHE

    bot = SimpleNamespace(set_my_commands=AsyncMock())
    application = SimpleNamespace(bot=bot, bot_data={})
    monkeypatch.setattr(main, "db_execute", lambda *args, **kwargs: [])

    asyncio.run(main.post_init(application))

    commands = {item.command for item in bot.set_my_commands.await_args.args[0]}
    assert {
        "start", "help", "briefing", "addop", "delop", "oplist", "opon", "opoff",
        "opcheck", "proxy_status",
    } == commands
    assert application.bot_data[KEY_HIST_FAILURE_CACHE] == {}
    assert not {"add", "del", "list", "on", "off", "check"} & commands
