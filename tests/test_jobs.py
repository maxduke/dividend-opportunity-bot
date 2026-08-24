import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_check_job_skips_overlapping_run(monkeypatch):
    from src import jobs

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_run(context):
        entered.set()
        await release.wait()

    runner = AsyncMock(side_effect=blocking_run)
    monkeypatch.setattr(jobs, "_check_rules_job", runner)

    async def exercise():
        context = SimpleNamespace(bot_data={})
        first = asyncio.create_task(jobs.check_rules_job(context))
        await entered.wait()
        await jobs.check_rules_job(context)
        release.set()
        await first

    asyncio.run(exercise())
    runner.assert_awaited_once()


def test_intraday_job_stops_before_market_or_data_access_when_disabled(monkeypatch):
    from src import jobs

    monkeypatch.setattr(jobs, "ENABLE_INTRADAY_MONITOR", False)
    monkeypatch.setattr(
        jobs,
        "is_market_hours",
        lambda: (_ for _ in ()).throw(AssertionError("market check should not run")),
    )

    asyncio.run(jobs._check_rules_job(SimpleNamespace(bot_data={})))
