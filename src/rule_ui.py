"""Rule cards and snapshot navigation for Telegram; callbacks are owner-scoped."""

import html
import json
import logging
import math
from dataclasses import fields
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from .config import DATA_QUALITY_LABELS, OPPORTUNITY_LEVEL_LABELS, SCORING_MODE_LABELS
from .database import db_execute, delete_opportunity_rule, is_whitelisted
from .opportunity import (
    OpportunitySnapshot, evaluate_opportunity, format_opportunity_chunks,
    record_rule_evaluation, save_opportunity_snapshot,
)

logger = logging.getLogger(__name__)
TZ = ZoneInfo('Asia/Shanghai')


def button(label, user_id, action, value):
    return InlineKeyboardButton(label, callback_data=f'op:{user_id}:{action}:{value}')


def owned_rule(user_id, rule_id):
    return db_execute(
        'SELECT * FROM opportunity_rules WHERE id = ? AND user_id = ?',
        (rule_id, user_id), fetchone=True, swallow_errors=False,
    )


def rule_page(user_id, page=0):
    count = db_execute('SELECT COUNT(*) AS n FROM opportunity_rules WHERE user_id = ?',
                       (user_id,), fetchone=True, swallow_errors=False)['n']
    if not count:
        return '您还没有设置红利机会监控规则。使用 /addop 添加。', None
    page = max(0, min(page, count - 1))
    rule = db_execute('SELECT * FROM opportunity_rules WHERE user_id = ? ORDER BY id LIMIT 1 OFFSET ?',
                      (user_id, page), fetchone=True, swallow_errors=False)
    score = '暂无' if rule['last_score'] is None else f"{rule['last_score']:.0f}"
    name = html.escape(str(rule['asset_name'] or rule['asset_code'])[:120])
    benchmark = html.escape(str(rule['benchmark_name'] or rule['benchmark_code'])[:120])
    text = (
        f'<b>红利机会监控 · {page + 1}/{count}</b>\n\n'
        f"{name} ({rule['asset_code']}) · ID {rule['id']}\n"
        f"估值基准：{benchmark} ({rule['benchmark_code']})\n"
        f"状态：{'监控中' if rule['is_active'] else '已暂停（仍可手动查询）'}\n"
        f"最近评分：{score} · {OPPORTUNITY_LEVEL_LABELS.get(rule['last_level'], '暂无')}\n"
        f"告警阈值：{rule['min_score']:g}\n"
        f"规则更新：{html.escape(str(rule['updated_at']))}\n"
        '评分为最近一次观测，点击查询获取当前结果。'
    )
    rid = rule['id']
    rows = [
        [button('查询摘要', user_id, 'check', rid),
         button('暂停' if rule['is_active'] else '恢复', user_id, 'off' if rule['is_active'] else 'on', rid)],
        [button('修改阈值', user_id, 'threshold', rid), button('删除', user_id, 'delete', rid)],
    ]
    nav = []
    if page:
        nav.append(button('上一条', user_id, 'page', page - 1))
    if page + 1 < count:
        nav.append(button('下一条', user_id, 'page', page + 1))
    if nav:
        rows.append(nav)
    return text, InlineKeyboardMarkup(rows)


def summary(snapshot):
    def label(labels, value):
        return html.escape(labels.get(value, value) or '暂无')
    text = (
        f'<b>{html.escape(snapshot.asset_name[:120])} ({snapshot.asset_code})</b>\n'
        f'规则 {snapshot.rule_id} · 估值基准：{html.escape(snapshot.benchmark_name[:120])} ({snapshot.benchmark_code})\n'
        f'评分：<b>{snapshot.total_score:.0f}/100</b> · {label(OPPORTUNITY_LEVEL_LABELS, snapshot.level)}\n'
        f'估值 {snapshot.valuation_score:.0f}/50 · 长期 {snapshot.long_term_score:.0f}/30 · 战术 {snapshot.tactical_score:.0f}/20\n'
        f'模式：{label(SCORING_MODE_LABELS, snapshot.scoring_mode)}\n'
        f'数据：{label(DATA_QUALITY_LABELS, snapshot.data_quality)}\n'
        f'价格日期：{html.escape(snapshot.technical_price_date or "暂无")}\n'
        f'估值日期：{html.escape(snapshot.valuation_date or "暂无")}\n'
        f'计算时间：{html.escape(snapshot.snapshot_at)}'
    )
    if snapshot.technical_price_basis == 'unavailable':
        text += '\n⚠️ 部分评分：不包含 MA200、52 周回撤和 RSI。'
    elif snapshot.technical_price_basis == 'qfq_history_close':
        text += '\n⚠️ 使用历史收盘价，未使用实时行情。'
    if snapshot.data_quality != 'OK' and snapshot.data_notes:
        text += '\n' + html.escape(snapshot.data_notes[0][:300])
    return text


async def run_query(message, context, user_id, rules):
    status = await message.reply_text('正在计算红利机会评分，请稍候...')
    succeeded = 0
    for rule in rules:
        try:
            snapshot = await evaluate_opportunity(rule, context)
            snapshot_id = save_opportunity_snapshot(snapshot, critical=True)
            record_rule_evaluation(rule['id'], snapshot)
            markup = InlineKeyboardMarkup([[button('完整明细', user_id, 'details', snapshot_id)]])
            await message.reply_html(summary(snapshot), reply_markup=markup)
            succeeded += 1
        except Exception:
            logger.exception('手动查询规则 %s 失败', rule['id'])
            await message.reply_text(
                f"规则 {rule['id']} 查询失败，请稍后重试。",
                reply_markup=InlineKeyboardMarkup([[button('重试本条', user_id, 'check', rule['id'])]]),
            )
    await status.edit_text(f'查询完成：成功 {succeeded} 条，失败 {len(rules) - succeeded} 条。')


def set_threshold(user_id, rule_id, value):
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError('threshold out of range')
    if owned_rule(user_id, rule_id) is None:
        return False
    db_execute('UPDATE opportunity_rules SET min_score = ?, updated_at = ? WHERE id = ? AND user_id = ?',
               (score, datetime.now(TZ).isoformat(), rule_id, user_id), swallow_errors=False)
    return True


def delete_confirmation(user_id, rule):
    return (
        f"确认删除规则 {rule['id']}（{rule['asset_code']}）？\n该规则的历史快照也会删除，无法撤销。",
        InlineKeyboardMarkup([[button('确认删除', user_id, 'confirm', rule['id']),
                               button('取消', user_id, 'page', 0)]]),
    )


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=markup)
    except BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise


async def rule_callback(update, context):
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        _, owner, action, raw_value = query.data.split(':')
        value = int(raw_value)
    except (ValueError, AttributeError):
        await query.answer('按钮无效，请重新使用 /oplist。', show_alert=True)
        return
    if owner != str(user_id) or not is_whitelisted(user_id):
        await query.answer('此按钮仅限原用户使用，且需要白名单权限。', show_alert=True)
        return
    if value < 0 or value > 2**63 - 1:
        await query.answer('按钮参数无效。', show_alert=True)
        return
    await query.answer()
    try:
        if action == 'page':
            await _edit(query, *rule_page(user_id, value))
            return
        if action == 'details':
            saved = db_execute('''SELECT s.*, r.asset_code, r.asset_name, r.benchmark_code, r.benchmark_name
                FROM opportunity_snapshots s JOIN opportunity_rules r ON r.id = s.rule_id
                WHERE s.id = ? AND r.user_id = ?''',
                (value, user_id), fetchone=True, swallow_errors=False)
            if saved is None:
                await _edit(query, '该结果已删除或不属于您，请重新查询。')
                return
            data = dict(saved)
            data['asset_name'] = data['asset_name'] or data['asset_code']
            data['benchmark_name'] = data['benchmark_name'] or data['benchmark_code']
            data['data_notes'] = json.loads(data['data_notes'] or '[]')
            snapshot = OpportunitySnapshot(**{f.name: data[f.name] for f in fields(OpportunitySnapshot) if f.name in data})
            # Read exactly the summary's persisted snapshot: expanding never fetches providers.
            for chunk in format_opportunity_chunks(snapshot):
                await query.message.reply_html(chunk)
            return
        rule = owned_rule(user_id, value)
        if rule is None:
            await _edit(query, '该规则已删除或不属于您，请重新使用 /oplist。')
            return
        if action == 'check':
            await run_query(query.message, context, user_id, [rule])
        elif action in {'on', 'off'}:
            from .handlers import set_rule_active
            await set_rule_active(rule, user_id, context, action == 'on')
            page = db_execute('SELECT COUNT(*) AS n FROM opportunity_rules WHERE user_id = ? AND id < ?',
                              (user_id, value), fetchone=True, swallow_errors=False)['n']
            await _edit(query, *rule_page(user_id, page))
        elif action == 'threshold':
            await _edit(query, f"规则 {value} 当前阈值：{rule['min_score']:g}\n选择新阈值，或发送 /opthreshold {value} 分数（0–100）。",
                InlineKeyboardMarkup([[button(str(score), user_id, f'set{score}', value) for score in (60, 70, 80)],
                                      [button('返回列表', user_id, 'page', 0)]]))
        elif action in {'set60', 'set70', 'set80'}:
            set_threshold(user_id, value, action[3:])
            await _edit(query, f'✅ 规则 {value} 告警阈值已更新为 {action[3:]}。\n保留现有监控状态和历史记录。',
                        InlineKeyboardMarkup([[button('返回列表', user_id, 'page', 0)]]))
        elif action == 'delete':
            await _edit(query, *delete_confirmation(user_id, rule))
        elif action == 'confirm':
            delete_opportunity_rule(user_id, value)
            await _edit(query, f'✅ 规则 {value} 已删除。',
                        InlineKeyboardMarkup([[button('返回列表', user_id, 'page', 0)]]))
        else:
            await _edit(query, '按钮无效，请重新使用 /oplist。')
    except Exception:
        logger.exception('规则按钮处理失败 action=%s', action)
        await query.message.reply_text('操作未完成，请稍后重试或使用 /oplist 查看当前状态。')
