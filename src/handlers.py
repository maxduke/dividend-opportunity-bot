# -*- coding: utf-8 -*-

"""Telegram handlers for the dividend Opportunity product."""

import html
import logging
import sqlite3
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .config import (
    ADMIN_USER_ID,
    AKSHARE_PROXY_LOW_BALANCE_THRESHOLD,
    BRIEFING_TIMES_STR,
    CSI_DIVIDEND_YIELD_FIELD,
    ENABLE_AKSHARE_PROXY_PATCH,
    KEY_CACHE_DATE,
    KEY_HIST_CACHE,
    KEY_HIST_FAILURE_CACHE,
    OPPORTUNITY_ALERT_THRESHOLD,
    PRICE_ADJUSTMENT,
    REQUEST_INTERVAL_SECONDS,
    RSI_PERIOD,
    TECHNICAL_HISTORY_DAYS,
)
from .data_fetcher import (
    _fetch_single_realtime_quote,
    get_asset_name_with_cache,
    runtime_history_is_usable,
)
from .database import (
    add_to_whitelist,
    db_execute,
    is_whitelisted,
    remove_from_whitelist,
)
from .opportunity import (
    evaluate_opportunity,
    format_opportunity_chunks,
    record_rule_evaluation,
    save_opportunity_snapshot,
)
from .proxy_health import (
    POSITIVE,
    check_proxy_balance_async,
    next_balance_retry_at,
    notify_proxy_health,
    proxy_patch_active,
)
from .valuation_fetcher import backfill_cn10y, get_cached_valuation

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def whitelisted_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_whitelisted(update.effective_user.id):
            await update.message.reply_text("抱歉，您没有权限使用此机器人。")
            return
        return await func(update, context, *args, **kwargs)

    return wrapped


def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_USER_ID:
            await update.message.reply_text("抱歉，此命令仅限管理员使用。")
            return
        return await func(update, context, *args, **kwargs)

    return wrapped


@whitelisted_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        f"你好, {user.mention_html()}!\n\n"
        "这是一个红利机会监控机器人。\n"
        "使用 /help 查看所有可用命令。"
    )


@whitelisted_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = db_execute(
        "SELECT daily_briefing_enabled FROM whitelist WHERE user_id = ?",
        (user_id,),
        fetchone=True,
    )
    briefing_status = "开启" if row and row["daily_briefing_enabled"] else "关闭"
    help_text = f"""
<b>可用命令:</b>

<b>每日简报</b>
/briefing <code>on|off</code> - 开/关您的每日简报 (您当前: <b>{briefing_status}</b>)

<b>红利机会监控</b>
/addop <code>ASSET BENCHMARK [MIN_SCORE]</code> - 添加机会监控
/delop <code>ID</code> - 删除机会监控
/oplist - 查看机会监控
/opon <code>ID</code> / /opoff <code>ID</code> - 开关机会监控
/opcheck [ID] - 查询机会分数明细

<b>白名单管理 (仅限管理员)</b>
/add_w <code>ID</code> - 添加用户
/del_w <code>ID</code> - 移除用户
/list_w - 查看白名单
/refresh - 清空历史数据缓存
/proxy_status [refresh] - 查看 AKShare Proxy 状态

<b>全局配置:</b>
- RSI6 周期（Opportunity 战术因子）: <b>{RSI_PERIOD}</b>
- 技术价格: <b>{PRICE_ADJUSTMENT}</b>
- 请求间隔: <b>{REQUEST_INTERVAL_SECONDS}秒</b>
- 每日简报: <b>{BRIEFING_TIMES_STR}</b>
"""
    await update.message.reply_html(help_text)


@whitelisted_only
async def add_opportunity_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sent_message = None
    created_rule_id = None
    creation_complete = False
    try:
        if len(context.args) not in (2, 3):
            await update.message.reply_text(
                "命令格式错误。\n正确格式: /addop <asset_code> <benchmark_code> [min_score]"
            )
            return
        asset_code, benchmark_code = context.args[:2]
        benchmark_code = benchmark_code.upper()
        min_score = float(context.args[2]) if len(context.args) == 3 else OPPORTUNITY_ALERT_THRESHOLD
        if not 0 <= min_score <= 100:
            await update.message.reply_text("min_score 必须在 0 到 100 之间。")
            return
        if not (
            asset_code.isdigit()
            and benchmark_code.isdigit()
            and len(asset_code) == 6
            and len(benchmark_code) == 6
        ):
            await update.message.reply_text("asset_code 和 benchmark_code 必须是 6 位数字代码。")
            return
        if db_execute(
            """
            SELECT id FROM opportunity_rules
            WHERE user_id = ? AND asset_code = ? AND benchmark_code = ?
            """,
            (update.effective_user.id, asset_code, benchmark_code),
            fetchone=True,
        ):
            await update.message.reply_text("❌ 相同的资产—benchmark Opportunity Rule 已存在。")
            return

        sent_message = await update.message.reply_text(
            f"正在验证资产 {asset_code} 与估值基准 {benchmark_code}，请稍候..."
        )
        quote = await _fetch_single_realtime_quote(asset_code)
        price = quote.price if quote is not None else None
        if price is None:
            await sent_message.edit_text(f"❌ 无法获取资产 {asset_code} 的实时价格，请确认代码正确。")
            return

        valuation = await get_cached_valuation(benchmark_code, context.bot_data)
        if valuation is None:
            await sent_message.edit_text(
                "❌ 该 benchmark 当前无法通过中证估值接口获取股息率，\n"
                "因此无法创建完整的红利估值监控规则。"
            )
            return
        selected_yield = "dividend_yield1" if CSI_DIVIDEND_YIELD_FIELD == "股息率1" else "dividend_yield2"
        if valuation[selected_yield] is None:
            await sent_message.edit_text(
                "❌ 该 benchmark 当前无法通过中证估值接口获取股息率，\n"
                "因此无法创建完整的红利估值监控规则。"
            )
            return

        await sent_message.edit_text("已验证估值基准，正在同步所需的中国十年期国债历史...")
        await backfill_cn10y()
        asset_name = await get_asset_name_with_cache(asset_code, context)
        benchmark_name = str(valuation["benchmark_name"] or benchmark_code)
        now = datetime.now(SHANGHAI_TZ).isoformat()
        try:
            db_execute(
                """
                INSERT INTO opportunity_rules (
                    user_id, asset_code, asset_name, benchmark_code, benchmark_name,
                    min_score, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update.effective_user.id,
                    asset_code,
                    asset_name,
                    benchmark_code,
                    benchmark_name,
                    min_score,
                    now,
                    now,
                ),
                swallow_errors=False,
            )
        except sqlite3.IntegrityError:
            await sent_message.edit_text("❌ 相同的资产—benchmark Opportunity Rule 已存在。")
            return

        rule = db_execute(
            """
            SELECT * FROM opportunity_rules
            WHERE user_id = ? AND asset_code = ? AND benchmark_code = ?
            """,
            (update.effective_user.id, asset_code, benchmark_code),
            fetchone=True,
        )
        created_rule_id = rule["id"]
        snapshot = await evaluate_opportunity(rule, context, quote=quote, spot_price=price)
        save_opportunity_snapshot(snapshot)
        record_rule_evaluation(rule["id"], snapshot)
        creation_complete = True
        await sent_message.edit_text(
            "✅ Opportunity monitor created\n\n"
            f"Asset: {asset_name} ({asset_code})\n"
            f"Benchmark: {benchmark_name} ({benchmark_code})\n\n"
            f"Current Score: {snapshot.total_score:.0f} / 100\n"
            f"Level: {snapshot.level}"
        )
    except ValueError:
        await update.message.reply_text("min_score 必须是数字。")
    except Exception as exc:
        logger.exception("添加 Opportunity Rule 失败: %s", exc)
        if created_rule_id is not None and not creation_complete:
            db_execute("DELETE FROM opportunity_snapshots WHERE rule_id = ?", (created_rule_id,))
            db_execute(
                "DELETE FROM opportunity_rules WHERE id = ?",
                (created_rule_id,),
                swallow_errors=False,
            )
        if sent_message:
            await sent_message.edit_text("添加 Opportunity Rule 时发生内部错误。")
        else:
            await update.message.reply_text("添加 Opportunity Rule 时发生内部错误。")


@whitelisted_only
async def list_opportunity_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = db_execute(
        "SELECT * FROM opportunity_rules WHERE user_id = ? ORDER BY id",
        (update.effective_user.id,),
        fetchall=True,
    )
    if not rules:
        await update.message.reply_text("您还没有设置 Opportunity Rule。使用 /addop 添加。")
        return
    lines = ["<b>Opportunity Monitor 列表:</b>", ""]
    for rule in rules:
        icon = "🟢" if rule["is_active"] else "🔴"
        score = "N/A" if rule["last_score"] is None else f"{rule['last_score']:.0f}"
        asset_name = html.escape(str(rule["asset_name"] or rule["asset_code"]))
        benchmark_name = html.escape(str(rule["benchmark_name"] or rule["benchmark_code"]))
        lines.append(
            f"{icon} <b>ID: {rule['id']}</b>\n"
            f"  - {asset_name} (<code>{rule['asset_code']}</code>)\n"
            f"  - Benchmark: {benchmark_name} (<code>{rule['benchmark_code']}</code>)\n"
            f"  - Score: {score} | Level: {rule['last_level'] or 'N/A'}\n"
            f"  - 告警阈值: {rule['min_score']:.0f}\n"
        )
    await update.message.reply_html("\n".join(lines))


@whitelisted_only
async def check_opportunity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        try:
            rule_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("正确格式: /opcheck [rule_id]")
            return
        rules = db_execute(
            "SELECT * FROM opportunity_rules WHERE id = ? AND user_id = ? AND is_active = 1",
            (rule_id, user_id),
            fetchall=True,
        )
    else:
        rules = db_execute(
            "SELECT * FROM opportunity_rules WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,),
            fetchall=True,
        )
    if not rules:
        await update.message.reply_text("没有找到已激活的 Opportunity Rule。")
        return
    status = await update.message.reply_text("正在计算 Opportunity Score，请稍候...")
    cache = context.bot_data.get(KEY_HIST_CACHE, {})
    first = True
    for rule in rules:
        cached = cache.get(rule["asset_code"])
        snapshot = await evaluate_opportunity(
            rule,
            context,
            hist_df=(
                cached
                if runtime_history_is_usable(cached, TECHNICAL_HISTORY_DAYS)
                else None
            ),
        )
        save_opportunity_snapshot(snapshot)
        record_rule_evaluation(rule["id"], snapshot)
        for chunk in format_opportunity_chunks(snapshot):
            if first:
                await status.edit_text(chunk, parse_mode=ParseMode.HTML)
                first = False
            else:
                await update.message.reply_html(chunk)


@whitelisted_only
async def delete_opportunity_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rule_id = int(context.args[0])
        rule = db_execute(
            "SELECT id FROM opportunity_rules WHERE id = ? AND user_id = ?",
            (rule_id, update.effective_user.id),
            fetchone=True,
        )
        if not rule:
            await update.message.reply_text("未找到该 Opportunity Rule，或规则不属于您。")
            return
        db_execute(
            "DELETE FROM opportunity_snapshots WHERE rule_id = ?",
            (rule_id,),
            swallow_errors=False,
        )
        db_execute("DELETE FROM opportunity_rules WHERE id = ?", (rule_id,), swallow_errors=False)
        await update.message.reply_text(f"✅ Opportunity Rule ID: {rule_id} 已删除。")
    except (ValueError, IndexError):
        await update.message.reply_text("正确格式: /delop <rule_id>")


@whitelisted_only
async def toggle_opportunity_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.split()[0].lower()
    try:
        rule_id = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text(f"正确格式: {command} <rule_id>")
        return
    rule = db_execute(
        "SELECT id FROM opportunity_rules WHERE id = ? AND user_id = ?",
        (rule_id, update.effective_user.id),
        fetchone=True,
    )
    if not rule:
        await update.message.reply_text("未找到该 Opportunity Rule，或规则不属于您。")
        return
    active = 1 if command == "/opon" else 0
    if active:
        db_execute(
            """
            UPDATE opportunity_rules
            SET is_active = 1, last_score = NULL, last_level = NULL,
                last_alert_score = NULL, last_alert_level = NULL, last_alert_at = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (datetime.now(SHANGHAI_TZ).isoformat(), rule_id, update.effective_user.id),
            swallow_errors=False,
        )
    else:
        db_execute(
            "UPDATE opportunity_rules SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
            (datetime.now(SHANGHAI_TZ).isoformat(), rule_id, update.effective_user.id),
            swallow_errors=False,
        )
    await update.message.reply_text(f"✅ Opportunity Rule ID: {rule_id} 已{'开启' if active else '关闭'}。")


@whitelisted_only
async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        row = db_execute(
            "SELECT daily_briefing_enabled FROM whitelist WHERE user_id = ?",
            (user_id,),
            fetchone=True,
        )
        status = "开启" if row and row["daily_briefing_enabled"] else "关闭"
        await update.message.reply_html(
            f"您的每日简报当前为 <b>{status}</b> 状态。\n\n"
            "使用 <code>/briefing on</code> 或 <code>/briefing off</code> 来进行设置。"
        )
        return
    command = context.args[0].lower()
    if command == "on":
        db_execute("UPDATE whitelist SET daily_briefing_enabled = 1 WHERE user_id = ?", (user_id,))
        await update.message.reply_text("✅ 已为您开启每日收盘前简报功能。")
    elif command == "off":
        db_execute("UPDATE whitelist SET daily_briefing_enabled = 0 WHERE user_id = ?", (user_id,))
        await update.message.reply_text("✅ 已为您关闭每日收盘前简报功能。")
    else:
        await update.message.reply_text("指令格式错误。请使用 /briefing on 或 /briefing off。")


@admin_only
async def add_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        _, user_id_str = update.message.text.split()
        user_id = int(user_id_str)
        add_to_whitelist(user_id)
        await update.message.reply_text(f"✅ 用户 {user_id} 已添加到白名单。")
    except (ValueError, IndexError):
        await update.message.reply_text("命令格式错误。\n正确格式: /add_w <user_id>")


@admin_only
async def del_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        _, user_id_str = update.message.text.split()
        user_id = int(user_id_str)
        if user_id == ADMIN_USER_ID:
            await update.message.reply_text("❌ 不能将管理员从白名单中删除。")
            return
        remove_from_whitelist(user_id)
        await update.message.reply_text(f"✅ 用户 {user_id} 已从白名单中移除。")
    except (ValueError, IndexError):
        await update.message.reply_text("命令格式错误。\n正确格式: /del_w <user_id>")


@admin_only
async def list_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db_execute("SELECT * FROM whitelist", fetchall=True)
    if not users:
        await update.message.reply_text("白名单中没有任何用户。")
        return
    message = "<b>白名单用户列表:</b>\n\n"
    for user in users:
        admin = " (管理员)" if user["user_id"] == ADMIN_USER_ID else ""
        briefing = " (简报:开)" if user["daily_briefing_enabled"] else ""
        message += f"- <code>{user['user_id']}</code>{admin}{briefing}\n"
    await update.message.reply_html(message)


@admin_only
async def proxy_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args != ["refresh"]:
        await update.message.reply_text("正确格式: /proxy_status [refresh]")
        return
    status = await check_proxy_balance_async(force=bool(context.args))
    await notify_proxy_health(context.bot)
    checked_at = status.checked_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M %z")
    checked_at = f"{checked_at[:-2]}:{checked_at[-2:]}"
    balance = "N/A" if status.balance is None else f"{status.balance:g}"
    threshold = (
        "disabled"
        if AKSHARE_PROXY_LOW_BALANCE_THRESHOLD <= 0
        else f"{AKSHARE_PROXY_LOW_BALANCE_THRESHOLD:g}"
    )
    active = proxy_patch_active()
    history_state = (
        "available"
        if active and status.state == POSITIVE
        else "degraded" if ENABLE_AKSHARE_PROXY_PATCH else "direct provider"
    )
    lines = [
        "AKShare Proxy",
        "",
        f"Configured: {'YES' if ENABLE_AKSHARE_PROXY_PATCH else 'NO'}",
        f"Patch active: {'YES' if active else 'NO'}",
        f"Balance status: {status.state}",
        f"Balance: {balance}",
        f"Last checked: {checked_at}",
        f"Low-balance threshold: {threshold}",
        "",
        f"ETF adjusted-history state: {history_state}",
    ]
    if ENABLE_AKSHARE_PROXY_PATCH and status.state != POSITIVE:
        retry_at = next_balance_retry_at(status)
        if retry_at is not None:
            retry = retry_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M %z")
            lines.append(f"Next balance retry: {retry[:-2]}:{retry[-2:]}")
    if status.state == POSITIVE and not active and ENABLE_AKSHARE_PROXY_PATCH:
        lines.extend(
            [
                "",
                "Balance is now positive, but the proxy patch was not installed at startup.",
                "Restart the bot to activate it safely.",
            ]
        )
    await update.message.reply_text("\n".join(lines))


@admin_only
async def refresh_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data[KEY_HIST_CACHE] = {}
    context.bot_data[KEY_HIST_FAILURE_CACHE] = {}
    context.bot_data[KEY_CACHE_DATE] = None
    await update.message.reply_text("✅ 历史数据缓存已清空，下次检查时将重新获取。")
