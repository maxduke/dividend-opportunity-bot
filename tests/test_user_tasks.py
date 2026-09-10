import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Message, MessageEntity, Update, User
from telegram.ext import Application, CommandHandler

from src import database, handlers, jobs, rule_ui, user_tasks
from .test_rule_ui import rules_db, message, snapshot


def context():
    return SimpleNamespace(bot_data={})


async def done(ctx, uid=9):
    manager = user_tasks.task_manager(ctx)
    work = manager.active.get(uid) or manager.recent[uid]
    await asyncio.wait_for(work.task, 1)
    return work


def test_bounded_queue_duplicate_rejection_and_queued_cancellation(monkeypatch):
    monkeypatch.setattr(user_tasks, 'is_whitelisted', lambda uid: True)

    async def exercise():
        manager = user_tasks.UserTaskManager(concurrency=1, capacity=2)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(work):
            entered.set()
            await release.wait()
        first = await manager.submit(1, message(), '第一项', blocked)
        await asyncio.wait_for(entered.wait(), 1)
        queued_op = AsyncMock()
        queued = await manager.submit(2, message(), '第二项', queued_op)
        assert queued.state == '排队中'
        assert await manager.submit(1, message(), '重复', queued_op) is None
        full_message = message()
        assert await manager.submit(3, full_message, '已满', queued_op) is None
        assert '队列已满' in full_message.reply_text.await_args.args[0]
        assert not manager.cancel(2, first.token)
        assert manager.cancel(2, queued.token)
        await asyncio.wait_for(queued.task, 1)
        assert queued.state == '已取消'
        queued_op.assert_not_awaited()
        release.set()
        await first.task
        assert not manager.active
        replacement = await manager.submit(2, message(), '新任务', queued_op)
        assert not manager.cancel(2, queued.token)
        await replacement.task
    asyncio.run(exercise())


def test_shutdown_cancels_running_and_queued_work_without_results(monkeypatch):
    monkeypatch.setattr(user_tasks, 'is_whitelisted', lambda uid: True)

    async def exercise():
        manager = user_tasks.UserTaskManager(concurrency=1)
        entered = asyncio.Event()
        results = []

        async def blocked(work):
            entered.set()
            await asyncio.Event().wait()
            results.append('unexpected')
        first = await manager.submit(1, message(), '运行', blocked)
        await entered.wait()
        queued = await manager.submit(2, message(), '排队', blocked)
        app = SimpleNamespace(bot_data={user_tasks.TASK_MANAGER_KEY: manager})
        await asyncio.wait_for(user_tasks.stop_user_tasks(app), 1)
        assert first.task.done() and queued.task.done()
        assert not manager.active and not results
        assert await manager.submit(3, message(), '关闭后', blocked) is None
    asyncio.run(exercise())


def test_failure_is_reported_and_admission_recovers(monkeypatch):
    monkeypatch.setattr(user_tasks, 'is_whitelisted', lambda uid: True)

    async def exercise():
        manager = user_tasks.UserTaskManager()
        failed = await manager.submit(9, message(), '失败', AsyncMock(side_effect=RuntimeError('failure')))
        await failed.task
        assert failed.state == '失败' and 9 not in manager.active
        success = await manager.submit(9, message(), '重试', AsyncMock(return_value='已完成'))
        await success.task
        assert success.detail == '已完成'
    asyncio.run(exercise())


def test_slow_query_does_not_block_real_telegram_update_dispatch(rules_db, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(rule, ctx):
            entered.set()
            await release.wait()
            return snapshot()
        monkeypatch.setattr(rule_ui, 'evaluate_opportunity', blocked)
        reply_html = AsyncMock()
        monkeypatch.setattr(Message, 'reply_html', reply_html)
        monkeypatch.setattr(Message, 'reply_text', AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
        app = Application.builder().token('123:test').build()
        app._initialized = True
        app.bot._bot_user = User(123, '测试机器人', True, username='testbot')
        app.add_handler(CommandHandler('opcheck', handlers.check_opportunity_command))
        app.add_handler(CommandHandler('help', handlers.help_command))
        app.add_handler(CommandHandler('cancel', user_tasks.cancel_command))

        def update(text, uid=9):
            msg = Message(1, datetime.now(timezone.utc), chat=SimpleNamespace(id=uid), text=text,
                          from_user=User(uid, '测试用户', False),
                          entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))])
            msg.set_bot(app.bot)
            value = Update(1, message=msg)
            value.set_bot(app.bot)
            return value
        try:
            await asyncio.wait_for(app.process_update(update('/opcheck 1')), 1)
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(app.process_update(update('/help', uid=8)), 1)
            assert '可用命令' in reply_html.await_args.args[0]
            work = app.bot_data[user_tasks.TASK_MANAGER_KEY].active[9]
            await asyncio.wait_for(app.process_update(update('/cancel')), 1)
            await asyncio.wait_for(work.task, 1)
            assert work.state == '已取消'
            assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 0
        finally:
            release.set()
            await user_tasks.stop_user_tasks(app)
    asyncio.run(exercise())


@pytest.mark.parametrize('mutation', ['threshold', 'delete', 'pause', 'revoke'])
def test_changed_rule_or_permission_discards_background_query(rules_db, monkeypatch, mutation):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(rule, ctx):
            entered.set()
            await release.wait()
            return snapshot()
        monkeypatch.setattr(rule_ui, 'evaluate_opportunity', blocked)
        ctx, msg = context(), message()
        await rule_ui.run_query(msg, ctx, 9, [rule_ui.owned_rule(9, 1)])
        await asyncio.wait_for(entered.wait(), 1)
        work = user_tasks.task_manager(ctx).active[9]
        if mutation == 'threshold':
            rule_ui.set_threshold(9, 1, 80)
        elif mutation == 'delete':
            database.delete_opportunity_rule(9, 1)
        elif mutation == 'pause':
            await handlers.set_rule_active(rule_ui.owned_rule(9, 1), 9, ctx, False)
        else:
            database.remove_from_whitelist(9)
        release.set()
        await asyncio.wait_for(work.task, 1)
        msg.reply_html.assert_not_awaited()
        assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 0
        if mutation == 'revoke':
            assert work.state == '已停止'
        else:
            assert '跳过 1 条' in work.detail
    asyncio.run(exercise())


def test_pause_while_resume_is_computing_wins_even_when_already_paused(rules_db, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(rule, ctx):
            entered.set()
            await release.wait()
            return snapshot()
        monkeypatch.setattr(handlers, 'evaluate_opportunity', blocked)
        ctx = context()
        await handlers.set_rule_active(rule_ui.owned_rule(9, 1), 9, ctx, False)
        await handlers.start_resume(message(), ctx, 9, rule_ui.owned_rule(9, 1))
        await asyncio.wait_for(entered.wait(), 1)
        await handlers.set_rule_active(rule_ui.owned_rule(9, 1), 9, ctx, False)
        release.set()
        await done(ctx)
        assert rule_ui.owned_rule(9, 1)['is_active'] == 0
        assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 0
    asyncio.run(exercise())


def test_cancel_creation_leaves_no_partial_rule(rules_db, monkeypatch):
    monkeypatch.setattr(handlers, '_fetch_single_realtime_quote', AsyncMock(return_value=SimpleNamespace(price=1.0)))
    monkeypatch.setattr(handlers, 'get_cached_valuation', AsyncMock(return_value={'dividend_yield2': 5, 'benchmark_name': '红利'}))
    monkeypatch.setattr(handlers, 'backfill_cn10y', AsyncMock())
    monkeypatch.setattr(handlers, 'get_asset_name_with_cache', AsyncMock(return_value='ETF'))

    async def exercise():
        entered = asyncio.Event()

        async def blocked(rule, ctx, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            return snapshot()
        monkeypatch.setattr(handlers, 'evaluate_opportunity', blocked)
        ctx = context()
        ctx.args = ['515180', '000922']
        update = SimpleNamespace(effective_user=SimpleNamespace(id=9), message=message())
        await handlers.add_opportunity_rule_command(update, ctx)
        await asyncio.wait_for(entered.wait(), 1)
        manager = user_tasks.task_manager(ctx)
        work = manager.active[9]
        manager.cancel(9)
        await asyncio.wait_for(work.task, 1)
        assert work.state == '已取消'
        assert rules_db.execute("SELECT COUNT(*) FROM opportunity_rules WHERE asset_code='515180'").fetchone()[0] == 0
        assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 0
    asyncio.run(exercise())


def test_scheduled_evaluation_cannot_publish_after_threshold_edit(rules_db, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return snapshot()
        monkeypatch.setattr(jobs, 'evaluate_opportunity', blocked)
        send = AsyncMock()
        monkeypatch.setattr(jobs, '_send_opportunity_alert', send)
        task = asyncio.create_task(jobs._evaluate_opportunity_rules(context(), [rule_ui.owned_rule(9, 1)],
            {'510301': object()}, {}, datetime.fromisoformat('2026-09-10T14:50:00+08:00')))
        await asyncio.wait_for(entered.wait(), 1)
        rule_ui.set_threshold(9, 1, 80)
        release.set()
        await asyncio.wait_for(task, 1)
        send.assert_not_awaited()
        assert rule_ui.owned_rule(9, 1)['last_score'] is None
    asyncio.run(exercise())


def test_cancel_button_rejects_other_user_and_stale_token(rules_db):
    async def exercise():
        entered = asyncio.Event()

        async def blocked(work):
            entered.set()
            await asyncio.Event().wait()
        ctx = context()
        manager = user_tasks.task_manager(ctx)
        work = await manager.submit(9, message(), '任务', blocked)
        await entered.wait()
        query = SimpleNamespace(data=f'task:9:{work.token}', answer=AsyncMock())
        await user_tasks.cancel_callback(SimpleNamespace(effective_user=SimpleNamespace(id=8), callback_query=query), ctx)
        assert not work.cancel_requested
        query.data = 'task:9:old-token'
        await user_tasks.cancel_callback(SimpleNamespace(effective_user=SimpleNamespace(id=9), callback_query=query), ctx)
        assert not work.cancel_requested
        query.data = f'task:9:{work.token}'
        await user_tasks.cancel_callback(SimpleNamespace(effective_user=SimpleNamespace(id=9), callback_query=query), ctx)
        await work.task
        assert work.state == '已取消'
    asyncio.run(exercise())


def test_cancelled_provider_wait_drops_result_after_thread_finishes(rules_db, monkeypatch):
    import threading
    from src.data_fetcher import _call_akshare

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        consumed = []

        def provider():
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            loop.call_soon_threadsafe(finished.set)
            return 'late-result'

        async def operation(work):
            result = await _call_akshare(provider)
            work.check()
            consumed.append(result)
        manager = user_tasks.UserTaskManager()
        work = await manager.submit(9, message(), '数据查询', operation)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            manager.cancel(9)
            await asyncio.wait_for(work.task, 1)
            assert work.state == '已取消' and not consumed
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
        assert not consumed
    asyncio.run(exercise())


def test_cancellation_after_first_result_keeps_it_and_stops_remaining_rules(rules_db, monkeypatch):
    async def exercise():
        entered = asyncio.Event()
        count = 0

        async def evaluate(rule, ctx):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
                await asyncio.Event().wait()
            return snapshot(rule['id'])
        monkeypatch.setattr(rule_ui, 'evaluate_opportunity', evaluate)
        ctx, msg = context(), message()
        await rule_ui.run_query(msg, ctx, 9, [rule_ui.owned_rule(9, 1), rule_ui.owned_rule(9, 2)])
        await asyncio.wait_for(entered.wait(), 1)
        manager = user_tasks.task_manager(ctx)
        work = manager.active[9]
        manager.cancel(9)
        await work.task
        assert rules_db.execute('SELECT rule_id FROM opportunity_snapshots').fetchall()[0]['rule_id'] == 1
        assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 1
        assert msg.reply_html.await_count == 1 and count == 2
    asyncio.run(exercise())


def test_status_and_cancel_commands_are_user_scoped(rules_db):
    async def exercise():
        ctx = context()
        entered = asyncio.Event()

        async def blocked(work):
            await work.progress('正在处理第 1 条。')
            entered.set()
            await asyncio.Event().wait()
        work = await user_tasks.task_manager(ctx).submit(9, message(), '查询', blocked)
        await entered.wait()
        owner = SimpleNamespace(effective_user=SimpleNamespace(id=9), message=message())
        other = SimpleNamespace(effective_user=SimpleNamespace(id=8), message=message())
        await user_tasks.task_command(owner, ctx)
        assert '第 1 条' in owner.message.reply_text.await_args.args[0]
        await user_tasks.task_command(other, ctx)
        assert '没有任务' in other.message.reply_text.await_args.args[0]
        await user_tasks.cancel_command(other, ctx)
        assert not work.cancel_requested
        await user_tasks.cancel_command(owner, ctx)
        await work.task
    asyncio.run(exercise())


def test_shutdown_during_task_admission_does_not_start_operation(monkeypatch):
    monkeypatch.setattr(user_tasks, 'is_whitelisted', lambda uid: True)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        manager = user_tasks.UserTaskManager()
        operation = AsyncMock()

        async def reply(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(edit_text=AsyncMock())
        pending = asyncio.create_task(manager.submit(9, SimpleNamespace(reply_text=reply), '任务', operation))
        await entered.wait()
        await manager.shutdown()
        release.set()
        assert await pending is None
        assert not manager.active
        operation.assert_not_awaited()
    asyncio.run(exercise())


def test_rule_revision_migrates_without_losing_existing_rule(monkeypatch):
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE opportunity_rules (id INTEGER PRIMARY KEY, user_id INTEGER,
        min_score REAL, is_active INTEGER, updated_at TEXT)''')
    conn.execute("INSERT INTO opportunity_rules VALUES (7, 9, 68.5, 0, '2026-09-10')")
    conn.commit()
    monkeypatch.setattr(database, '_conn', conn)
    try:
        database.db_init()
        database.add_to_whitelist(9)
        rule = rule_ui.owned_rule(9, 7)
        assert rule['revision'] == 0 and rule['min_score'] == 68.5 and rule['is_active'] == 0
        rule_ui.set_threshold(9, 7, 80)
        assert not database.rule_is_current(rule)
        assert rule_ui.owned_rule(9, 7)['revision'] == 1
    finally:
        conn.close()
