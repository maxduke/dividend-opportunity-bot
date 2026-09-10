import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import database, handlers, rule_ui
from src.user_tasks import task_manager
from src.opportunity import OpportunitySnapshot, save_opportunity_snapshot


@pytest.fixture
def rules_db(monkeypatch):
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    monkeypatch.setattr(database, '_conn', conn)
    database.db_init()
    database.add_to_whitelist(9)
    database.add_to_whitelist(8)
    for rid, owner in [(1, 9), (2, 9), (3, 8)]:
        conn.execute('''INSERT INTO opportunity_rules
            (id, user_id, asset_code, asset_name, benchmark_code, benchmark_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, '000922', '中证红利', '2026-09-10', '2026-09-10')''',
            (rid, owner, str(510300 + rid), '<红利&ETF>',))
    conn.commit()
    yield conn
    conn.close()


def message():
    return SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock(
        return_value=SimpleNamespace(edit_text=AsyncMock())))


def callback(data, uid=9):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=message())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), callback_query=query)


def snapshot(rid=1, score=72):
    return OpportunitySnapshot(rid, str(510300 + rid), '<红利&ETF>', '000922', '中证红利',
        '2026-09-10T14:50:00+08:00', total_score=score, level='MODERATE', price=1.1,
        spot_price=1.2, technical_price_basis='qfq_history_close', technical_price_date='2026-09-09',
        valuation_date='2026-09-09', data_notes=['行情时间不可用'])


def test_rule_cards_paginate_and_clamp_deleted_pages(rules_db):
    text, markup = rule_ui.rule_page(9)
    assert '1/2' in text and 'ID 1' in text
    assert '&lt;红利&amp;ETF&gt;' in text
    assert markup.inline_keyboard[-1][0].callback_data == 'op:9:page:1'
    text, _ = rule_ui.rule_page(9, 10000)
    assert '2/2' in text and 'ID 2' in text
    database.delete_opportunity_rule(9, 2)
    text, _ = rule_ui.rule_page(9, 1)
    assert '1/1' in text and 'ID 1' in text
    database.delete_opportunity_rule(9, 1)
    assert rule_ui.rule_page(9)[1] is None
    assert rules_db.execute('SELECT COUNT(*) FROM opportunity_rules WHERE user_id=8').fetchone()[0] == 1


def test_card_size_is_bounded_for_long_provider_names(rules_db):
    rules_db.execute('UPDATE opportunity_rules SET asset_name=?, benchmark_name=?', ('<&>'*3000, '<&>'*3000))
    text, _ = rule_ui.rule_page(9)
    assert len(text) < 3800


@pytest.mark.parametrize('data,uid,revoke', [
    ('op:9:confirm:1', 8, False), ('op:9:confirm:1', 9, True),
    ('op:9:confirm:3', 9, False), ('op:9:set80:3', 9, False),
    ('op:9:page:999999999999999999999999', 9, False),
    ('op:9:confirm:invalid', 9, False),
])
def test_callbacks_reject_wrong_owner_or_invalid_data(rules_db, data, uid, revoke):
    if revoke:
        database.remove_from_whitelist(9)
    update = callback(data, uid)
    asyncio.run(rule_ui.rule_callback(update, SimpleNamespace()))
    assert rules_db.execute('SELECT COUNT(*) FROM opportunity_rules').fetchone()[0] == 3
    assert rules_db.execute('SELECT min_score FROM opportunity_rules WHERE id=3').fetchone()[0] == 60


def test_delete_requires_confirmation_and_preserves_other_user(rules_db):
    save_opportunity_snapshot(snapshot(), critical=True)
    update = callback('op:9:delete:1')
    asyncio.run(rule_ui.rule_callback(update, SimpleNamespace()))
    assert rule_ui.owned_rule(9, 1) is not None
    assert '无法撤销' in update.callback_query.edit_message_text.await_args.args[0]
    update = callback('op:9:confirm:1')
    asyncio.run(rule_ui.rule_callback(update, SimpleNamespace()))
    assert rule_ui.owned_rule(9, 1) is None
    assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 0
    assert rule_ui.owned_rule(8, 3) is not None


def test_delete_rolls_back_snapshots_if_rule_delete_fails(rules_db):
    save_opportunity_snapshot(snapshot(), critical=True)
    rules_db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON opportunity_rules BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    rules_db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        database.delete_opportunity_rule(9, 1)
    assert rule_ui.owned_rule(9, 1) is not None
    assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 1


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', '-1', '101', 'oops'])
def test_invalid_threshold_does_not_modify_rule(rules_db, value):
    with pytest.raises(ValueError):
        rule_ui.set_threshold(9, 1, value)
    assert rule_ui.owned_rule(9, 1)['min_score'] == 60


def test_threshold_edit_preserves_paused_state_and_snapshots(rules_db):
    save_opportunity_snapshot(snapshot(), critical=True)
    rules_db.execute("UPDATE opportunity_rules SET is_active=0, last_score=72, last_level='MODERATE' WHERE id=1")
    rules_db.commit()
    assert rule_ui.set_threshold(9, 1, '68.5')
    rule = rule_ui.owned_rule(9, 1)
    assert (rule['min_score'], rule['is_active'], rule['last_score']) == (68.5, 0, 72)
    assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 1
    assert not rule_ui.set_threshold(8, 1, '80')


def test_details_expand_original_snapshot_without_fetching(rules_db, monkeypatch):
    evaluate = AsyncMock(return_value=snapshot())
    monkeypatch.setattr(rule_ui, 'evaluate_opportunity', evaluate)
    msg = message()
    async def query():
        context = SimpleNamespace(bot_data={})
        await rule_ui.run_query(msg, context, 9, [rule_ui.owned_rule(9, 1)])
        await (task_manager(context).active.get(9) or task_manager(context).recent[9]).task
    asyncio.run(query())
    data = msg.reply_html.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
    save_opportunity_snapshot(snapshot(score=90), critical=True)
    update = callback(data)
    asyncio.run(rule_ui.rule_callback(update, SimpleNamespace()))
    detail = '\n'.join(call.args[0] for call in update.callback_query.message.reply_html.await_args_list)
    assert '72 / 100' in detail and '90 / 100' not in detail
    assert '现价：1.200' in detail
    evaluate.assert_awaited_once()
    other = callback(data.replace('op:9:', 'op:8:'), uid=8)
    asyncio.run(rule_ui.rule_callback(other, SimpleNamespace()))
    other.callback_query.message.reply_html.assert_not_awaited()


def test_query_isolates_failures_and_returns_retry_button(rules_db, monkeypatch):
    monkeypatch.setattr(rule_ui, 'evaluate_opportunity', AsyncMock(side_effect=[RuntimeError('outage'), snapshot(2)]))
    msg = message()
    async def query():
        context = SimpleNamespace(bot_data={})
        await rule_ui.run_query(msg, context, 9,
            [rule_ui.owned_rule(9, 1), rule_ui.owned_rule(9, 2)])
        await (task_manager(context).active.get(9) or task_manager(context).recent[9]).task
    asyncio.run(query())
    assert '成功 1 条，失败 1 条' in msg.reply_text.return_value.edit_text.await_args.args[0]
    retry = msg.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert retry.callback_data == 'op:9:check:1'
    msg.reply_html.assert_awaited_once()


def test_paused_rule_is_queryable_by_id(rules_db, monkeypatch):
    rules_db.execute('UPDATE opportunity_rules SET is_active=0 WHERE id=1')
    rules_db.commit()
    query = AsyncMock()
    monkeypatch.setattr(rule_ui, 'run_query', query)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=9), message=message())
    asyncio.run(handlers.check_opportunity_command(update, SimpleNamespace(args=['1'])))
    assert query.await_args.args[3][0]['id'] == 1
    asyncio.run(handlers.check_opportunity_command(update, SimpleNamespace(args=[])))
    assert [rule['id'] for rule in query.await_args.args[3]] == [2]


def test_on_off_buttons_share_baseline_and_are_idempotent(rules_db, monkeypatch):
    evaluate = AsyncMock(return_value=snapshot())
    monkeypatch.setattr(handlers, 'evaluate_opportunity', evaluate)
    context = SimpleNamespace(bot_data={})
    asyncio.run(rule_ui.rule_callback(callback('op:9:off:1'), context))
    assert rule_ui.owned_rule(9, 1)['is_active'] == 0
    evaluate.assert_not_awaited()
    async def resume():
        await rule_ui.rule_callback(callback('op:9:on:1'), context)
        await (task_manager(context).active.get(9) or task_manager(context).recent[9]).task
        await rule_ui.rule_callback(callback('op:9:on:1'), context)
    asyncio.run(resume())
    rule = rule_ui.owned_rule(9, 1)
    assert rule['is_active'] == 1 and rule['last_score'] == 72
    evaluate.assert_awaited_once()


def test_threshold_command_and_button_preserve_history(rules_db):
    save_opportunity_snapshot(snapshot(), critical=True)
    msg = message()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=9), message=msg)
    asyncio.run(handlers.threshold_opportunity_command(update, SimpleNamespace(args=['1', '69.5'])))
    assert rule_ui.owned_rule(9, 1)['min_score'] == 69.5
    asyncio.run(rule_ui.rule_callback(callback('op:9:set80:1'), SimpleNamespace()))
    assert rule_ui.owned_rule(9, 1)['min_score'] == 80
    assert rules_db.execute('SELECT COUNT(*) FROM opportunity_snapshots').fetchone()[0] == 1


def test_delete_command_only_prompts_and_cancel_keeps_rule(rules_db):
    msg = message()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=9), message=msg)
    asyncio.run(handlers.delete_opportunity_rule_command(update, SimpleNamespace(args=['1'])))
    assert rule_ui.owned_rule(9, 1) is not None
    assert '确认删除' in msg.reply_text.await_args.args[0]
    markup = msg.reply_text.await_args.kwargs['reply_markup']
    cancel = markup.inline_keyboard[0][1].callback_data
    asyncio.run(rule_ui.rule_callback(callback(cancel), SimpleNamespace()))
    assert rule_ui.owned_rule(9, 1) is not None


def test_deleted_snapshot_details_and_missing_names_are_handled(rules_db):
    sid = save_opportunity_snapshot(snapshot(), critical=True)
    rules_db.execute('UPDATE opportunity_rules SET asset_name=NULL, benchmark_name=NULL WHERE id=1')
    rules_db.commit()
    update = callback(f'op:9:details:{sid}')
    asyncio.run(rule_ui.rule_callback(update, SimpleNamespace()))
    update.callback_query.message.reply_html.assert_awaited()
    database.delete_opportunity_rule(9, 1)
    update = callback(f'op:9:details:{sid}')
    asyncio.run(rule_ui.rule_callback(update, SimpleNamespace()))
    assert '已删除' in update.callback_query.edit_message_text.await_args.args[0]
    update.callback_query.message.reply_html.assert_not_awaited()


def test_summary_marks_partial_technical_score():
    value = snapshot()
    value.technical_price_basis = 'unavailable'
    text = rule_ui.summary(value)
    assert '部分评分' in text and '不包含 MA200' in text
