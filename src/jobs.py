# -*- coding: utf-8 -*-

import asyncio
import html
import logging
import math
import random
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Union

import pytz
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes

from .config import (
    ENABLE_DAILY_BRIEFING,
    ENABLE_OPPORTUNITY_MONITOR,
    HIST_FETCH_DAYS,
    MAX_NOTIFICATIONS_PER_TRIGGER,
    RANDOM_DELAY_MAX_SECONDS,
    REQUEST_INTERVAL_SECONDS,
    RSI_PERIOD,
    TECHNICAL_HISTORY_DAYS,
)
from .database import db_execute
from .data_fetcher import (
    _fetch_all_spot_data,
    calculate_rsi,
    ensure_daily_history_cache,
    get_history_data_cached,
    get_prices_for_rsi,
    history_failure_is_fresh,
)
from .market import is_market_hours, is_trading_day
from .opportunity import (
    evaluate_opportunity,
    format_opportunity_chunks,
    record_rule_alert,
    record_rule_evaluation,
    save_opportunity_snapshot,
    should_send_opportunity_alert,
    snapshot_should_persist,
)

logger = logging.getLogger(__name__)


def _in_range(rsi_value: Union[float, int, None], rsi_min: float, rsi_max: float) -> bool:
    if rsi_value is None:
        return False
    try:
        value = float(rsi_value)
        if math.isnan(value) or math.isinf(value):
            return False
        return rsi_min <= value <= rsi_max
    except (TypeError, ValueError):
        return False


NotificationEntry = Tuple[sqlite3.Row, float, bool]
SHANGHAI_TZ = pytz.timezone('Asia/Shanghai')


def _today_shanghai_str(now: datetime = None) -> str:
    """返回上海时区的监控日期字符串。"""
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = SHANGHAI_TZ.localize(current)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    return current.strftime('%Y-%m-%d')


def _reset_stale_notification_counts(today_str: str) -> int:
    """跨自然日重置通知次数，避免昨日触发中的规则阻止今日再次提醒。"""
    stale_rules = db_execute(
        """
        SELECT id FROM rules
        WHERE notification_count > 0
          AND (last_notification_date IS NULL OR last_notification_date <> ?)
        """,
        (today_str,),
        fetchall=True,
    ) or []
    if not stale_rules:
        return 0

    db_execute(
        """
        UPDATE rules
        SET notification_count = 0
        WHERE notification_count > 0
          AND (last_notification_date IS NULL OR last_notification_date <> ?)
        """,
        (today_str,),
    )
    return len(stale_rules)


def _build_notification_chunks(
    rules_for_user: List[NotificationEntry],
    max_len: int = 3500
) -> List[Tuple[str, List[NotificationEntry]]]:
    """
    将单用户触发规则分块，避免 Telegram 4096 字符上限导致整条消息发送失败。
    """
    header = "🎯 <b>RSI 警报汇总</b> 🎯\n\n"
    chunks: List[Tuple[str, List[NotificationEntry]]] = []
    current_parts: List[str] = [header]
    current_rules: List[NotificationEntry] = []

    for rule, current_rsi, should_increment in rules_for_user:
        safe_asset_name = html.escape(str(rule['asset_name'] or "未知资产"))
        current_count = int(rule['notification_count'] or 0)
        if should_increment:
            count_text = f"{current_count + 1}/{MAX_NOTIFICATIONS_PER_TRIGGER}"
            count_suffix = ""
        else:
            shown_count = min(current_count, MAX_NOTIFICATIONS_PER_TRIGGER)
            count_text = f"{shown_count}/{MAX_NOTIFICATIONS_PER_TRIGGER}"
            count_suffix = "（已达上限，仅汇总展示）"

        section = (
            f"• <b>{safe_asset_name} ({rule['asset_code']})</b>\n"
            f"  RSI({RSI_PERIOD}): <b>{current_rsi:.2f}</b>\n"
            f"  目标区间: <code>{rule['rsi_min']} - {rule['rsi_max']}</code>\n"
            f"  通知次数: <b>{count_text}</b>{count_suffix}\n\n"
        )

        tentative = "".join(current_parts) + section
        if len(tentative) > max_len and current_rules:
            chunks.append(("".join(current_parts).strip(), current_rules.copy()))
            current_parts = [header, section]
            current_rules = [(rule, current_rsi, should_increment)]
        else:
            current_parts.append(section)
            current_rules.append((rule, current_rsi, should_increment))

    if current_rules:
        chunks.append(("".join(current_parts).strip(), current_rules.copy()))
    return chunks


def _split_message(text: str, max_len: int = 3800) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > max_len:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --- 后台监控任务 ---

async def check_rules_job(context: ContextTypes.DEFAULT_TYPE):
    """Run at most one market check at a time to avoid request storms."""
    lock = context.bot_data.setdefault("check_rules_job_lock", asyncio.Lock())
    if lock.locked():
        logger.warning("上一次规则检查仍在运行，本次跳过以避免重复请求。")
        return
    async with lock:
        await _check_rules_job(context)


async def _check_rules_job(context: ContextTypes.DEFAULT_TYPE):
    if not is_market_hours():
        return
    if RANDOM_DELAY_MAX_SECONDS > 0:
        delay = random.uniform(0, RANDOM_DELAY_MAX_SECONDS)
        logger.info(f"应用启动延迟: {delay:.2f}秒")
        await asyncio.sleep(delay)

    logger.info("交易时间，开始执行规则检查...")
    now = datetime.now(SHANGHAI_TZ)
    today_str = _today_shanghai_str(now)
    reset_count = _reset_stale_notification_counts(today_str)
    if reset_count:
        logger.info(f"已重置 {reset_count} 条跨日通知计数器，当前监控日期: {today_str}。")

    active_rules = db_execute("SELECT * FROM rules WHERE is_active = 1", fetchall=True) or []
    active_opportunity_rules = (
        db_execute("SELECT * FROM opportunity_rules WHERE is_active = 1", fetchall=True) or []
        if ENABLE_OPPORTUNITY_MONITOR else []
    )
    if not active_rules and not active_opportunity_rules:
        return

    all_codes = sorted({rule['asset_code'] for rule in active_rules} | {rule['asset_code'] for rule in active_opportunity_rules})
    hist_data_cache = ensure_daily_history_cache(context, now)
    opportunity_codes = {rule['asset_code'] for rule in active_opportunity_rules}
    codes_to_fetch_hist = [
        code for code in all_codes
        if (
            code not in hist_data_cache
            or (
                code in opportunity_codes
                and hist_data_cache[code].attrs.get("technical_history_days", 0) < TECHNICAL_HISTORY_DAYS
            )
        ) and not history_failure_is_fresh(context, code, now)
    ]

    if codes_to_fetch_hist:
        logger.info(f"需要为 {len(codes_to_fetch_hist)} 个新资产顺序获取历史数据...")
        for index, code in enumerate(codes_to_fetch_hist):
            logger.debug(f"正在获取 {code} 的历史数据...")
            required_days = TECHNICAL_HISTORY_DAYS if code in opportunity_codes else HIST_FETCH_DAYS
            data = await get_history_data_cached(context, code, required_days, now)
            if data is not None and not data.empty:
                hist_data_cache[code] = data
            if index < len(codes_to_fetch_hist) - 1 and REQUEST_INTERVAL_SECONDS > 0:
                logger.debug(f"应用请求间隔: {REQUEST_INTERVAL_SECONDS}秒")
                await asyncio.sleep(REQUEST_INTERVAL_SECONDS)

    spot_data, success = await _fetch_all_spot_data(context, all_codes)
    if not success:
        return

    rsi_by_code: Dict[str, float] = {}
    for code in all_codes:
        hist_df = hist_data_cache.get(code)
        spot_price = spot_data.get(code)
        if hist_df is None or spot_price is None:
            continue
        prices = get_prices_for_rsi(hist_df, spot_price)
        current_rsi = calculate_rsi(prices)
        if current_rsi is not None:
            rsi_by_code[code] = current_rsi

    pending_notifications: Dict[int, List[NotificationEntry]] = defaultdict(list)
    for rule in active_rules:
        asset_code = rule['asset_code']
        current_rsi = rsi_by_code.get(asset_code)
        if current_rsi is None:
            logger.warning(f"RSI 计算失败，跳过规则: {rule['asset_name']}({asset_code})")
            continue

        logger.debug(f"检查: {rule['asset_name']}({asset_code}) | RSI({RSI_PERIOD}): {current_rsi}")
        is_triggered = _in_range(current_rsi, rule['rsi_min'], rule['rsi_max'])
        last_notified_rsi_in_range = _in_range(rule['last_notified_rsi'], rule['rsi_min'], rule['rsi_max'])

        if is_triggered:
            should_increment = rule['notification_count'] < MAX_NOTIFICATIONS_PER_TRIGGER
            pending_notifications[rule['user_id']].append((rule, current_rsi, should_increment))
            if should_increment:
                continue

        if not is_triggered and last_notified_rsi_in_range:
            logger.info(f"离开区间: {asset_code} | 重置通知计数器。")
            db_execute(
                "UPDATE rules SET last_notified_rsi = ?, notification_count = 0, last_notification_date = NULL WHERE id = ?",
                (current_rsi, rule['id'])
            )
        elif is_triggered:
            db_execute("UPDATE rules SET last_notified_rsi = ? WHERE id = ?", (current_rsi, rule['id']))

    for user_id, triggered_rules in pending_notifications.items():
        if not any(should_increment for _, _, should_increment in triggered_rules):
            continue

        triggered_rules_sorted = sorted(
            triggered_rules,
            key=lambda item: (item[0]['asset_code'], item[0]['rsi_min'], item[0]['rsi_max'], item[0]['id'])
        )
        message_chunks = _build_notification_chunks(triggered_rules_sorted)
        for message, rules_in_chunk in message_chunks:
            sent = False
            for _ in range(2):
                try:
                    await context.bot.send_message(chat_id=user_id, text=message, parse_mode=ParseMode.HTML)
                    sent = True
                    break
                except RetryAfter as e:
                    wait_seconds = int(getattr(e, "retry_after", 1)) + 1
                    logger.warning(f"发送通知触发限流，{wait_seconds}秒后重试。用户: {user_id}")
                    await asyncio.sleep(wait_seconds)
                except Exception as e:
                    logger.error(f"向用户 {user_id} 发送通知失败: {e}")
                    break

            if sent:
                for rule, current_rsi, should_increment in rules_in_chunk:
                    if not should_increment:
                        continue
                    logger.info(
                        f"已发送通知: {rule['asset_code']} | 用户: {user_id} | "
                        f"(第 {rule['notification_count'] + 1} 次)"
                    )
                    db_execute(
                        """
                        UPDATE rules
                        SET last_notified_rsi = ?,
                            notification_count = notification_count + 1,
                            last_notification_date = ?
                        WHERE id = ?
                        """,
                        (current_rsi, today_str, rule['id'])
                    )

    if active_opportunity_rules:
        await _check_opportunity_rules(context, active_opportunity_rules, spot_data, hist_data_cache, now)


async def _send_opportunity_alert(context, rule, snapshot, reason) -> bool:
    for message in format_opportunity_chunks(snapshot, alert_reason=reason):
        sent = False
        for _ in range(2):
            try:
                await context.bot.send_message(
                    chat_id=rule['user_id'],
                    text=message,
                    parse_mode=ParseMode.HTML,
                )
                sent = True
                break
            except RetryAfter as exc:
                wait_seconds = int(getattr(exc, "retry_after", 1)) + 1
                logger.warning("Opportunity 告警触发限流，%s秒后重试。用户: %s", wait_seconds, rule['user_id'])
                await asyncio.sleep(wait_seconds)
            except Exception as exc:
                logger.error("向用户 %s 发送 Opportunity 告警失败: %s", rule['user_id'], exc)
                break
        if not sent:
            return False
    return True


def _opportunity_alerts_today(rule_id: int, today: datetime) -> int:
    row = db_execute(
        """
        SELECT COUNT(*) AS count FROM opportunity_snapshots
        WHERE rule_id = ? AND alert_sent = 1 AND snapshot_at >= ?
        """,
        (rule_id, today.strftime('%Y-%m-%d')),
        fetchone=True,
    )
    return int(row['count']) if row else 0


async def _check_opportunity_rules(context, rules, spot_data, hist_data_cache, now):
    for rule in rules:
        try:
            spot_price = spot_data.get(rule['asset_code'])
            if spot_price is None:
                logger.warning("实时价格缺失，跳过 Opportunity Rule: %s", rule['id'])
                continue
            snapshot = await evaluate_opportunity(
                rule,
                context,
                spot_price=spot_price,
                hist_df=hist_data_cache.get(rule['asset_code']),
            )
            alerts_today = _opportunity_alerts_today(rule['id'], now)
            should_alert, reason = should_send_opportunity_alert(
                rule,
                snapshot,
                now=now,
                alerts_today=alerts_today,
            )
            sent = await _send_opportunity_alert(context, rule, snapshot, reason) if should_alert else False
            if sent:
                logger.info("[OPPORTUNITY] 已发送告警 rule=%s reason=%s", rule['id'], reason)
            if should_alert and not sent:
                # Keep the previous baseline so a transient Telegram failure can retry next loop.
                continue
            if snapshot_should_persist(rule['id'], snapshot, alert_sent=sent):
                save_opportunity_snapshot(snapshot, alert_sent=sent)
            record_rule_evaluation(rule['id'], snapshot, now)
            if sent:
                record_rule_alert(rule['id'], snapshot, now)
        except Exception as exc:
            logger.exception("[OPPORTUNITY] 评估规则 %s 失败: %s", rule['id'], exc)


async def daily_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_DAILY_BRIEFING:
        return
    tz = SHANGHAI_TZ
    now = datetime.now(tz)
    if not is_trading_day(now):
        logger.info(f"今天 ({now.strftime('%Y-%m-%d')}) 非交易日，跳过每日简报。")
        return

    logger.info("开始执行每日收盘监控简报任务...")
    enabled_users_rows = db_execute(
        "SELECT user_id FROM whitelist WHERE daily_briefing_enabled = 1", fetchall=True
    )
    if not enabled_users_rows:
        return

    enabled_user_ids = {row['user_id'] for row in enabled_users_rows}
    placeholders = ','.join('?' for _ in enabled_user_ids)
    all_briefing_rules = db_execute(
        f"SELECT * FROM rules WHERE is_active = 1 AND user_id IN ({placeholders})",
        tuple(enabled_user_ids),
        fetchall=True,
    ) or []
    all_opportunity_rules = (
        db_execute(
            f"SELECT * FROM opportunity_rules WHERE is_active = 1 AND user_id IN ({placeholders})",
            tuple(enabled_user_ids),
            fetchall=True,
        ) or []
        if ENABLE_OPPORTUNITY_MONITOR else []
    )
    if not all_briefing_rules and not all_opportunity_rules:
        return

    all_unique_codes = sorted(
        {rule['asset_code'] for rule in all_briefing_rules}
        | {rule['asset_code'] for rule in all_opportunity_rules}
    )

    spot_data, success = await _fetch_all_spot_data(context, all_unique_codes)
    if not success:
        logger.error("执行每日简报任务时获取数据失败，任务中止。")
        return

    hist_data_cache = ensure_daily_history_cache(context, now)
    opportunity_codes = {rule['asset_code'] for rule in all_opportunity_rules}
    for code in all_unique_codes:
        cached = hist_data_cache.get(code)
        required_days = TECHNICAL_HISTORY_DAYS if code in opportunity_codes else HIST_FETCH_DAYS
        needs_technical = code in opportunity_codes and (
            cached is None or cached.attrs.get("technical_history_days", 0) < TECHNICAL_HISTORY_DAYS
        )
        if cached is None or needs_technical:
            await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
            hist_df = await get_history_data_cached(context, code, required_days, now)
            if hist_df is not None and not hist_df.empty:
                hist_data_cache[code] = hist_df

    rsi_results: Dict[str, Union[str, float]] = {}
    for code in all_unique_codes:
        spot_price = spot_data.get(code)
        if spot_price is None:
            rsi_results[code] = "N/A"
            continue

        hist_df = hist_data_cache.get(code)

        prices = get_prices_for_rsi(hist_df, spot_price)
        rsi_value = calculate_rsi(prices) if prices is not None else "N/A"
        rsi_results[code] = rsi_value

    opportunity_results = {}
    for rule in all_opportunity_rules:
        try:
            spot_price = spot_data.get(rule['asset_code'])
            if spot_price is None:
                logger.warning("每日简报实时价格缺失，跳过 Opportunity Rule: %s", rule['id'])
                continue
            snapshot = await evaluate_opportunity(
                rule,
                context,
                spot_price=spot_price,
                hist_df=hist_data_cache.get(rule['asset_code']),
            )
            save_opportunity_snapshot(snapshot)
            record_rule_evaluation(rule['id'], snapshot, now)
            opportunity_results[rule['id']] = snapshot
        except Exception as exc:
            logger.exception("每日简报计算 Opportunity Rule %s 失败: %s", rule['id'], exc)

    today_str_display = now.strftime('%Y年%m月%d日')
    rules_by_user = defaultdict(list)
    for rule in all_briefing_rules:
        rules_by_user[rule['user_id']].append(("rsi", rule))
    for rule in all_opportunity_rules:
        rules_by_user[rule['user_id']].append(("opportunity", rule))

    for user_id, user_rules in rules_by_user.items():
        message = f"📰 <b>收盘监控简报 ({today_str_display})</b>\n\n"
        rsi_rules = [rule for kind, rule in user_rules if kind == "rsi"]
        opportunity_rules = [rule for kind, rule in user_rules if kind == "opportunity"]
        if rsi_rules:
            message += "📈 <b>RSI Monitor</b>\n\n"
            user_rules_by_code = defaultdict(list)
            for rule in rsi_rules:
                user_rules_by_code[rule['asset_code']].append(rule)
            for code, code_rules in sorted(user_rules_by_code.items()):
                asset_name = code_rules[0]['asset_name']
                rsi_val = rsi_results.get(code)
                if isinstance(rsi_val, float):
                    is_triggered = any(rule['rsi_min'] <= rsi_val <= rule['rsi_max'] for rule in code_rules)
                    icon = "🎯" if is_triggered else "▪️"
                    rsi_str = f"<b>{rsi_val:.2f}</b>"
                else:
                    icon = "❓"
                    rsi_str = "查询失败"
                message += f"{icon} <b>{asset_name}</b> (<code>{code}</code>)\n"
                message += f"  - 收盘 RSI({RSI_PERIOD}): {rsi_str}\n"
                for rule in code_rules:
                    message += f"  - 监控区间: {rule['rsi_min']} - {rule['rsi_max']}\n"
                message += "\n"
        if opportunity_rules:
            message += "💰 <b>Opportunity Monitor</b>\n\n"
            for rule in opportunity_rules:
                snapshot = opportunity_results.get(rule['id'])
                if snapshot is None:
                    message += f"❓ {rule['asset_name']} ({rule['asset_code']}) 查询失败\n\n"
                    continue
                message += (
                    f"{snapshot.level} <b>{html.escape(snapshot.asset_name)}</b> ({snapshot.asset_code})\n"
                    f"  Score: <b>{snapshot.total_score:.0f}</b> | Level: {snapshot.level}\n"
                    f"  DY: {snapshot.dividend_yield_used:.2f}%\n"
                    if snapshot.dividend_yield_used is not None else
                    f"{snapshot.level} <b>{html.escape(snapshot.asset_name)}</b> ({snapshot.asset_code})\n"
                    f"  Score: <b>{snapshot.total_score:.0f}</b> | Level: {snapshot.level}\n"
                    f"  DY: N/A\n"
                )
                message += f"  DY-CN10Y: {snapshot.dividend_bond_spread:.2f}pp\n" if snapshot.dividend_bond_spread is not None else "  DY-CN10Y: N/A\n"
                message += f"  MA200 deviation: {snapshot.ma200_deviation * 100:.1f}%\n" if snapshot.ma200_deviation is not None else "  MA200 deviation: N/A\n"
                message += f"  52W DD: {snapshot.drawdown_52w * 100:.1f}%\n" if snapshot.drawdown_52w is not None else "  52W DD: N/A\n"
                message += f"  RSI({RSI_PERIOD}): {snapshot.rsi6:.1f}\n\n" if snapshot.rsi6 is not None else f"  RSI({RSI_PERIOD}): N/A\n\n"
        try:
            for message_chunk in _split_message(message):
                await context.bot.send_message(chat_id=user_id, text=message_chunk, parse_mode=ParseMode.HTML)
            logger.info(f"已成功向用户 {user_id} 发送每日简报。")
        except Forbidden:
            logger.warning(f"无法向用户 {user_id} 发送每日简报，可能已被禁用。")
        except Exception as e:
            logger.error(f"向用户 {user_id} 发送每日简报时发生未知错误: {e}")
