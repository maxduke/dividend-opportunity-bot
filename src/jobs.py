# -*- coding: utf-8 -*-

"""Scheduled Opportunity monitoring jobs.

The old standalone RSI-rule scheduler was intentionally removed. RSI6 is
calculated inside Opportunity evaluation and these jobs only operate on
``opportunity_rules``.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes

from .config import (
    DATA_QUALITY_LABELS,
    ENABLE_INTRADAY_MONITOR,
    OPPORTUNITY_LEVEL_LABELS,
    REQUEST_INTERVAL_SECONDS,
    RSI_PERIOD,
    SCORING_MODE_LABELS,
    TECHNICAL_HISTORY_DAYS,
)
from .data_fetcher import (
    _fetch_all_realtime_quotes,
    ensure_daily_history_cache,
    get_history_data_cached,
    history_failure_is_fresh,
    runtime_history_is_usable,
)
from .database import db_execute
from .market import ensure_trade_days_loaded, is_market_hours, is_trading_day
from .opportunity import (
    evaluate_opportunity,
    format_opportunity_alert,
    record_rule_alert,
    record_rule_evaluation,
    save_opportunity_snapshot,
    should_send_opportunity_alert,
    snapshot_should_persist,
)
from .utils import split_message

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _retry_after_seconds(exc: RetryAfter) -> int:
    retry_after = exc.retry_after
    if isinstance(retry_after, timedelta):
        retry_after = retry_after.total_seconds()
    return int(retry_after) + 1


async def _load_opportunity_history(context, codes, now):
    """Populate one shared qfq history cache, respecting failure cooldowns."""
    cache = ensure_daily_history_cache(context, now)
    missing = [
        code for code in codes
        if not runtime_history_is_usable(cache.get(code), TECHNICAL_HISTORY_DAYS, now)
        and not history_failure_is_fresh(context, code, now)
    ]
    for index, code in enumerate(missing):
        if index and REQUEST_INTERVAL_SECONDS > 0:
            await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
        logger.debug("正在获取 %s 的 qfq 历史数据...", code)
        history = await get_history_data_cached(context, code, TECHNICAL_HISTORY_DAYS, now)
        if history is not None and not history.empty:
            cache[code] = history
    return cache


async def check_opportunity_job(context: ContextTypes.DEFAULT_TYPE):
    """Run at most one intraday Opportunity check at a time."""
    lock = context.bot_data.setdefault("check_opportunity_job_lock", asyncio.Lock())
    if lock.locked():
        logger.warning("上一次 Opportunity 检查仍在运行，本次跳过以避免重复请求。")
        return
    async with lock:
        await _check_opportunity_job(context)


async def _check_opportunity_job(context: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_INTRADAY_MONITOR:
        return
    await ensure_trade_days_loaded()
    if not is_market_hours():
        return

    rules = db_execute(
        """
        SELECT opportunity_rules.*
        FROM opportunity_rules
        INNER JOIN whitelist ON whitelist.user_id = opportunity_rules.user_id
        WHERE opportunity_rules.is_active = 1
        """,
        fetchall=True,
    ) or []
    if not rules:
        return

    now = datetime.now(SHANGHAI_TZ)
    codes = sorted({rule["asset_code"] for rule in rules})
    history = await _load_opportunity_history(context, codes, now)
    quotes, success = await _fetch_all_realtime_quotes(context, codes)
    if not success:
        return
    await _evaluate_opportunity_rules(context, rules, quotes, history, now)


async def _send_opportunity_alert(context, rule, snapshot, reason) -> bool:
    message = format_opportunity_alert(snapshot, reason=reason)
    for attempt in range(2):
        try:
            await context.bot.send_message(
                chat_id=rule["user_id"], text=message, parse_mode=ParseMode.HTML
            )
            return True
        except RetryAfter as exc:
            if attempt:
                return False
            wait_seconds = _retry_after_seconds(exc)
            logger.warning(
                "Opportunity 告警触发限流，%s秒后重试。用户: %s",
                wait_seconds,
                rule["user_id"],
            )
            await asyncio.sleep(wait_seconds)
        except Forbidden:
            db_execute(
                "UPDATE opportunity_rules SET is_active = 0, updated_at = ? WHERE user_id = ?",
                (datetime.now(SHANGHAI_TZ).isoformat(), rule["user_id"]),
                swallow_errors=False,
            )
            logger.warning(
                "无法向用户 %s 发送 Opportunity 告警，已停用该用户的监控规则。",
                rule["user_id"],
            )
            return False
        except Exception as exc:
            logger.error("向用户 %s 发送 Opportunity 告警失败: %s", rule["user_id"], exc)
            return False
    return False


def _opportunity_alerts_today(rule_id: int, today: datetime) -> int:
    row = db_execute(
        """
        SELECT COUNT(*) AS count FROM opportunity_snapshots
        WHERE rule_id = ? AND alert_sent = 1 AND snapshot_at >= ?
        """,
        (rule_id, today.strftime("%Y-%m-%d")),
        fetchone=True,
    )
    return int(row["count"]) if row else 0


async def _evaluate_opportunity_rules(context, rules, quotes, history, now):
    for rule in rules:
        try:
            quote = quotes.get(rule["asset_code"])
            if quote is None:
                logger.warning("实时价格缺失，跳过 Opportunity Rule: %s", rule["id"])
                continue
            snapshot = await evaluate_opportunity(
                rule,
                context,
                quote=quote,
                hist_df=(
                    cached
                    if runtime_history_is_usable(
                        (cached := history.get(rule["asset_code"])),
                        TECHNICAL_HISTORY_DAYS,
                        now,
                    )
                    else None
                ),
            )
            alerts_today = _opportunity_alerts_today(rule["id"], now)
            should_alert, reason = should_send_opportunity_alert(
                rule, snapshot, now=now, alerts_today=alerts_today
            )
            sent = await _send_opportunity_alert(context, rule, snapshot, reason) if should_alert else False
            if should_alert and not sent:
                # Preserve the old baseline so a transient Telegram failure can retry.
                continue
            if snapshot_should_persist(rule["id"], snapshot, alert_sent=sent):
                save_opportunity_snapshot(snapshot, alert_sent=sent)
            record_rule_evaluation(rule["id"], snapshot, now)
            if sent:
                record_rule_alert(rule["id"], snapshot, now)
                logger.info("[OPPORTUNITY] 已发送告警 rule=%s reason=%s", rule["id"], reason)
        except Exception as exc:
            logger.exception("[OPPORTUNITY] 评估规则 %s 失败: %s", rule["id"], exc)


async def daily_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(SHANGHAI_TZ)
    await ensure_trade_days_loaded(now)
    if not is_trading_day(now):
        logger.info("今天 (%s) 非交易日，跳过每日简报。", now.strftime("%Y-%m-%d"))
        return

    enabled_users = db_execute(
        "SELECT user_id FROM whitelist WHERE daily_briefing_enabled = 1", fetchall=True
    ) or []
    if not enabled_users:
        return
    user_ids = {row["user_id"] for row in enabled_users}
    placeholders = ",".join("?" for _ in user_ids)
    rules = db_execute(
        f"SELECT * FROM opportunity_rules WHERE is_active = 1 AND user_id IN ({placeholders})",
        tuple(user_ids),
        fetchall=True,
    ) or []
    if not rules:
        return

    codes = sorted({rule["asset_code"] for rule in rules})
    quotes, success = await _fetch_all_realtime_quotes(context, codes)
    if not success:
        logger.error("执行每日简报任务时获取数据失败，任务中止。")
        return
    history = await _load_opportunity_history(context, codes, now)

    snapshots = {}
    for rule in rules:
        quote = quotes.get(rule["asset_code"])
        if quote is None:
            continue
        try:
            snapshot = await evaluate_opportunity(
                rule,
                context,
                quote=quote,
                hist_df=(
                    cached
                    if runtime_history_is_usable(
                        (cached := history.get(rule["asset_code"])),
                        TECHNICAL_HISTORY_DAYS,
                        now,
                    )
                    else None
                ),
            )
            save_opportunity_snapshot(snapshot)
            record_rule_evaluation(rule["id"], snapshot, now)
            snapshots[rule["id"]] = snapshot
        except Exception as exc:
            logger.exception("每日简报计算 Opportunity Rule %s 失败: %s", rule["id"], exc)

    rules_by_user = defaultdict(list)
    for rule in rules:
        rules_by_user[rule["user_id"]].append(rule)
    today = now.strftime("%Y年%m月%d日")
    for user_id, user_rules in rules_by_user.items():
        message = f"📰 <b>收盘前红利机会简报（{today}）</b>\n\n"
        if any(
            snapshots.get(rule["id"]) is not None
            and snapshots[rule["id"]].technical_price_basis == "unavailable"
            for rule in user_rules
        ):
            message += (
                "⚠️ 一个或多个资产的技术数据已降级。\n"
                "请使用 /opcheck 查看详情。\n\n"
            )
        for rule in user_rules:
            snapshot = snapshots.get(rule["id"])
            if snapshot is None:
                message += f"❓ {html.escape(str(rule['asset_name']))} ({rule['asset_code']}) 查询失败\n\n"
                continue
            dy = (
                f"{snapshot.dividend_yield_used:.2f}%"
                if snapshot.dividend_yield_used is not None else "暂无"
            )
            spread = (
                f"{snapshot.dividend_bond_spread:.2f} 个百分点"
                if snapshot.dividend_bond_spread is not None else "暂无"
            )
            ma = (
                f"{snapshot.ma200_deviation * 100:.1f}%"
                if snapshot.ma200_deviation is not None else "暂无"
            )
            drawdown = (
                f"{snapshot.drawdown_52w * 100:.1f}%"
                if snapshot.drawdown_52w is not None else "暂无"
            )
            rsi = f"{snapshot.rsi6:.1f}" if snapshot.rsi6 is not None else "暂无"
            technically_degraded = snapshot.technical_price_basis == "unavailable"
            displayed_quality = "DEGRADED" if technically_degraded else snapshot.data_quality
            message += (
                f"{OPPORTUNITY_LEVEL_LABELS.get(snapshot.level, snapshot.level)} <b>{html.escape(snapshot.asset_name)}</b> ({snapshot.asset_code})\n"
                f"  评分：<b>{snapshot.total_score:.0f}</b> | 等级：{OPPORTUNITY_LEVEL_LABELS.get(snapshot.level, snapshot.level)}\n"
                f"  模式：<code>{html.escape(SCORING_MODE_LABELS.get(snapshot.scoring_mode, snapshot.scoring_mode))}</code> | 数据：<code>{html.escape(DATA_QUALITY_LABELS.get(displayed_quality, displayed_quality))}</code>\n"
                f"  技术数据：{'不可用' if technically_degraded else '可用'}\n"
                f"  截至：价格 {html.escape(snapshot.technical_price_date or '暂无')} | 估值 {html.escape(snapshot.valuation_date or '暂无')}\n"
                f"  股息率：{dy}\n"
                f"  股息率—国债利差：{spread}\n"
                f"  MA200 偏离度：{ma}\n"
                f"  52 周回撤：{drawdown}\n"
                f"  RSI({RSI_PERIOD})：{rsi}\n\n"
            )
        try:
            for chunk in split_message(message):
                for attempt in range(2):
                    try:
                        await context.bot.send_message(
                            chat_id=user_id, text=chunk, parse_mode=ParseMode.HTML
                        )
                        break
                    except RetryAfter as exc:
                        if attempt:
                            raise
                        wait_seconds = _retry_after_seconds(exc)
                        logger.warning(
                            "每日简报触发限流，%s秒后重试。用户: %s",
                            wait_seconds,
                            user_id,
                        )
                        await asyncio.sleep(wait_seconds)
            logger.info("已成功向用户 %s 发送每日 Opportunity 简报。", user_id)
        except Forbidden:
            logger.warning("无法向用户 %s 发送每日简报，可能已被禁用。", user_id)
        except Exception as exc:
            logger.error("向用户 %s 发送每日简报时发生未知错误: %s", user_id, exc)
