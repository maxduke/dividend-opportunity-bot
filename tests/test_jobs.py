import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


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

    bot = SimpleNamespace(set_my_commands=AsyncMock())
    application = SimpleNamespace(bot=bot, bot_data={})
    monkeypatch.setattr(main, "db_execute", lambda *args, **kwargs: [])

    asyncio.run(main.post_init(application))

    commands = {item.command for item in bot.set_my_commands.await_args.args[0]}
    assert {"start", "help", "briefing", "addop", "delop", "oplist", "opon", "opoff", "opcheck"} == commands
    assert not {"add", "del", "list", "on", "off", "check"} & commands
