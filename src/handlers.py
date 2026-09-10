# -*- coding: utf-8 -*-

"""Telegram handlers for the dividend Opportunity product."""

import asyncio
import logging
import sqlite3
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .config import (
    ADMIN_USER_ID,
    AKSHARE_PROXY_LOW_BALANCE_THRESHOLD,
    BRIEFING_TIMES_STR,
    CSI_DIVIDEND_YIELD_FIELD,
    ENABLE_AKSHARE_PROXY_PATCH,
    ENABLE_INTRADAY_MONITOR,
    ETF_PREFIXES,
    KEY_CACHE_DATE,
    KEY_HIST_CACHE,
    KEY_HIST_FAILURE_CACHE,
    KEY_QUOTE_FAILURE_COUNTS,
    KEY_QUOTE_FAILURE_NOTIFIED,
    OPPORTUNITY_ALERT_THRESHOLD,
    OPPORTUNITY_LEVEL_LABELS,
    PRICE_ADJUSTMENT,
    PROXY_STATE_LABELS,
    REQUEST_INTERVAL_SECONDS,
    RSI_PERIOD,
    STOCK_PREFIXES,
)
from .data_fetcher import (
    _fetch_single_realtime_quote,
    get_asset_name_with_cache,
)
from .database import (
    add_to_whitelist,
    db_execute,
    is_whitelisted,
    remove_from_whitelist,
    rule_is_current,
)
from .opportunity import (
    evaluate_opportunity,
    record_rule_evaluation,
    save_opportunity_snapshot,
)
from .proxy_health import (
    POSITIVE,
    check_proxy_balance_async,
    next_balance_retry_at,
    notify_proxy_health,
    proxy_health_category,
    proxy_patch_active,
)
from .valuation_fetcher import backfill_cn10y, get_cached_valuation
from .user_tasks import AccessRevoked, task_manager

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _briefing_times():
    valid = []
    for value in BRIEFING_TIMES_STR.split(","):
        try:
            hour, minute = map(int, value.strip().split(":"))
            if 0 <= hour < 24 and 0 <= minute < 60:
                valid.append(f"{hour:02d}:{minute:02d}")
        except ValueError:
            continue
    return sorted(set(valid))


def _delivery_guidance(user_id):
    row = db_execute(
        "SELECT daily_briefing_enabled FROM whitelist WHERE user_id = ?",
        (user_id,), fetchone=True, swallow_errors=False,
    )
    enabled = bool(row and row["daily_briefing_enabled"])
    times = _briefing_times()
    lines = [f"盘中自动告警：{'开启' if ENABLE_INTRADAY_MONITOR else '关闭'}"]
    markup = None
    if not times:
        lines.append("每日简报：管理员尚未配置发送时间，请联系管理员开启。")
    else:
        lines.append(f"每日简报：{'开启' if enabled else '关闭'}")
        lines.append(f"发送安排：交易日 {'、'.join(times)}（上海时间）")
        if not enabled:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                "开启每日简报", callback_data=f"briefing_on:{user_id}"
            )]])
    if not ENABLE_INTRADAY_MONITOR and not (enabled and times):
        lines.append("当前仅支持手动查询，不会自动推送。使用 /opcheck 查看评分。")
    return "\n".join(lines), markup


async def enable_briefing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    if query.data != f"briefing_on:{user_id}" or not is_whitelisted(user_id):
        await query.answer("此按钮仅限原用户使用，且需要白名单权限。", show_alert=True)
        return
    if not _briefing_times():
        await query.answer("管理员尚未配置简报时间，请联系管理员。", show_alert=True)
        return
    db_execute(
        "UPDATE whitelist SET daily_briefing_enabled = 1 WHERE user_id = ?",
        (user_id,), swallow_errors=False,
    )
    await query.answer("已开启每日简报")
    await query.edit_message_reply_markup(reply_markup=None)
    guidance, _ = _delivery_guidance(user_id)
    await query.message.reply_text("✅ 已开启每日简报\n\n" + guidance)


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
<b>可用命令：</b>

<b>每日简报</b>
/briefing <code>on|off</code> - 开/关您的每日简报 (您当前: <b>{briefing_status}</b>)

<b>红利机会监控</b>
/addop <code>资产代码 估值基准代码 [最低评分]</code> - 添加机会监控
/delop <code>ID</code> - 删除机会监控
/oplist - 查看机会监控
/opon <code>ID</code> / /opoff <code>ID</code> - 开关机会监控
/opcheck [ID] - 查询摘要，按钮展开完整明细
/opthreshold <code>ID 分数</code> - 修改告警阈值（0–100）
/task - 查看当前或最近的后台任务
/cancel - 取消自己的当前后台任务

<b>白名单管理（仅限管理员）</b>
/add_w <code>ID</code> - 添加用户
/del_w <code>ID</code> - 移除用户
/list_w - 查看白名单
/refresh - 清空历史数据缓存
/proxy_status [refresh] - 查看 AKShare Proxy 状态

<b>全局配置：</b>
- RSI6 周期（红利机会战术因子）：<b>{RSI_PERIOD}</b>
- 技术价格: <b>{PRICE_ADJUSTMENT}</b>
- 请求间隔: <b>{REQUEST_INTERVAL_SECONDS}秒</b>
- 每日简报: <b>{BRIEFING_TIMES_STR}</b>
"""
    await update.message.reply_html(help_text)


@whitelisted_only
async def add_opportunity_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = tuple(context.args)
    await task_manager(context).submit(
        update.effective_user.id, update.message, "添加机会规则",
        lambda work: _add_opportunity_rule(update, context, args, work),
    )


async def _add_opportunity_rule(update, context, args, work=None):
    sent_message = work.message if work else None
    created_rule_id = None
    creation_complete = False

    async def report(text, reply_markup=None):
        if work:
            work.detail = text
            work.result_markup = reply_markup
            await work._notify(reply_markup or work.keyboard())
        elif sent_message is not None:
            await sent_message.edit_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)

    try:
        if len(args) not in (2, 3):
            await report(
                "命令格式错误。\n正确格式：/addop <资产代码> <估值基准代码> [最低评分]"
            )
            return
        asset_code, benchmark_code = args[:2]
        benchmark_code = benchmark_code.upper()
        min_score = float(args[2]) if len(args) == 3 else OPPORTUNITY_ALERT_THRESHOLD
        if not 0 <= min_score <= 100:
            await report("最低评分必须在 0 到 100 之间。")
            return
        if not (
            asset_code.isdigit()
            and benchmark_code.isdigit()
            and len(asset_code) == 6
            and len(benchmark_code) == 6
        ):
            await report("资产代码和估值基准代码必须是 6 位数字。")
            return
        if asset_code[0] not in STOCK_PREFIXES + ETF_PREFIXES:
            await report(
                f"❌ 暂不支持资产代码 {asset_code}，仅支持股票和 ETF 历史数据源覆盖的代码。"
            )
            return
        if db_execute(
            """
            SELECT id FROM opportunity_rules
            WHERE user_id = ? AND asset_code = ? AND benchmark_code = ?
            """,
            (update.effective_user.id, asset_code, benchmark_code),
            fetchone=True,
        ):
            await report("❌ 相同的资产—估值基准监控规则已存在。")
            return

        if sent_message is None:
            sent_message = await update.message.reply_text(
                f"正在验证资产 {asset_code} 与估值基准 {benchmark_code}，请稍候..."
            )
        if work:
            await work.progress("正在验证资产报价与估值基准。")
        fetch_lock = context.bot_data.setdefault("quote_fetch_lock", asyncio.Lock())
        async with fetch_lock:
            quote = await _fetch_single_realtime_quote(asset_code)
            if quote is not None:
                context.bot_data.setdefault(KEY_QUOTE_FAILURE_COUNTS, {}).pop(
                    asset_code, None
                )
                context.bot_data.setdefault(KEY_QUOTE_FAILURE_NOTIFIED, {}).pop(
                    asset_code, None
                )
        price = quote.price if quote is not None else None
        if price is None:
            await report(f"❌ 无法获取资产 {asset_code} 的实时价格，请确认代码正确。")
            return

        valuation = await get_cached_valuation(benchmark_code, context.bot_data)
        if valuation is None:
            await report(
                "❌ 该估值基准当前无法通过中证估值接口获取股息率，\n"
                "因此无法创建完整的红利估值监控规则。"
            )
            return
        selected_yield = "dividend_yield1" if CSI_DIVIDEND_YIELD_FIELD == "股息率1" else "dividend_yield2"
        if valuation[selected_yield] is None:
            await report(
                "❌ 该估值基准当前无法通过中证估值接口获取股息率，\n"
                "因此无法创建完整的红利估值监控规则。"
            )
            return

        await report("已验证估值基准，正在同步所需的中国十年期国债历史...")
        if work:
            await work.progress("正在同步国债历史。")
        backfill_lock = context.bot_data.setdefault("bond_backfill_lock", asyncio.Lock())
        async with backfill_lock:
            await backfill_cn10y()
        asset_name = await get_asset_name_with_cache(asset_code, context)
        benchmark_name = str(valuation["benchmark_name"] or benchmark_code)
        if work:
            await work.progress("正在计算初始评分，完成后才保存规则。")
        draft = dict(id=0, user_id=update.effective_user.id, asset_code=asset_code,
                     asset_name=asset_name, benchmark_code=benchmark_code,
                     benchmark_name=benchmark_name, min_score=min_score)
        snapshot = await evaluate_opportunity(draft, context, quote=quote, spot_price=price)
        if work:
            work.check()
        elif not is_whitelisted(update.effective_user.id):
            raise AccessRevoked
        now = datetime.now(SHANGHAI_TZ).isoformat()
        try:
            created_rule_id = db_execute(
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
                return_lastrowid=True,
            )
        except sqlite3.IntegrityError:
            await report("❌ 相同的资产—估值基准监控规则已存在。")
            return

        snapshot.rule_id = created_rule_id
        save_opportunity_snapshot(snapshot, critical=True)
        record_rule_evaluation(created_rule_id, snapshot)
        creation_complete = True
        guidance, markup = _delivery_guidance(update.effective_user.id)
        await report(
            "✅ 红利机会监控已创建\n\n"
            f"资产：{asset_name} ({asset_code})\n"
            f"估值基准：{benchmark_name} ({benchmark_code})\n\n"
            f"当前评分：{snapshot.total_score:.0f} / 100\n"
            f"机会等级：{OPPORTUNITY_LEVEL_LABELS.get(snapshot.level, snapshot.level)}\n\n"
            f"{guidance}\n\n"
            "提示：机器人只验证两端数据可用，不会自动验证资产实际跟踪该估值基准，请自行核对。",
            reply_markup=markup,
        )
    except AccessRevoked:
        raise
    except ValueError:
        await report("最低评分必须是数字。")
    except Exception as exc:
        logger.exception("添加 Opportunity Rule 失败: %s", exc)
        if created_rule_id is not None and not creation_complete:
            db_execute("DELETE FROM opportunity_snapshots WHERE rule_id = ?", (created_rule_id,))
            db_execute(
                "DELETE FROM opportunity_rules WHERE id = ?",
                (created_rule_id,),
                swallow_errors=False,
            )
        await report("添加红利机会监控规则时发生内部错误。")


@whitelisted_only
async def list_opportunity_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .rule_ui import rule_page

    text, markup = rule_page(update.effective_user.id)
    await update.message.reply_html(text, reply_markup=markup)


@whitelisted_only
async def check_opportunity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .rule_ui import owned_rule, run_query

    user_id = update.effective_user.id
    if context.args:
        try:
            if len(context.args) != 1:
                raise ValueError
            rule_id = int(context.args[0])
            if not 0 < rule_id <= 2**63 - 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("正确格式：/opcheck [规则 ID]")
            return
        rule = owned_rule(user_id, rule_id)
        rules = [rule] if rule is not None else []
    else:
        rules = db_execute(
            "SELECT * FROM opportunity_rules WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,), fetchall=True, swallow_errors=False,
        )
    if not rules:
        await update.message.reply_text("没有找到可查询的规则。暂停规则可使用 /opcheck ID 单独查询。")
        return
    await run_query(update.message, context, user_id, rules)


@whitelisted_only
async def threshold_opportunity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .rule_ui import set_threshold

    try:
        if len(context.args) != 2:
            raise ValueError
        rule_id = int(context.args[0])
        if not 0 < rule_id <= 2**63 - 1:
            raise ValueError
        updated = set_threshold(update.effective_user.id, rule_id, context.args[1])
    except ValueError:
        await update.message.reply_text("正确格式：/opthreshold <规则 ID> <0–100 的分数>")
        return
    await update.message.reply_text(
        f"✅ 规则 {rule_id} 告警阈值已更新为 {float(context.args[1]):g}。监控状态和历史记录已保留。"
        if updated else "未找到该规则，或规则不属于您。"
    )


@whitelisted_only
async def delete_opportunity_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .rule_ui import delete_confirmation, owned_rule

    try:
        if len(context.args) != 1:
            raise ValueError
        rule_id = int(context.args[0])
        if not 0 < rule_id <= 2**63 - 1:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("正确格式：/delop <规则 ID>")
        return
    rule = owned_rule(update.effective_user.id, rule_id)
    if rule is None:
        await update.message.reply_text("未找到该规则，或规则不属于您。")
        return
    text, markup = delete_confirmation(update.effective_user.id, rule)
    await update.message.reply_text(text, reply_markup=markup)


@whitelisted_only
async def toggle_opportunity_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.split()[0].lower().split("@", 1)[0]
    try:
        rule_id = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text(f"正确格式：{command} <规则 ID>")
        return
    rule = db_execute(
        "SELECT * FROM opportunity_rules WHERE id = ? AND user_id = ?",
        (rule_id, update.effective_user.id),
        fetchone=True,
    )
    if not rule:
        await update.message.reply_text("未找到该红利机会监控规则，或规则不属于您。")
        return
    active = 1 if command == "/opon" else 0
    if active and not rule["is_active"]:
        await start_resume(update.message, context, update.effective_user.id, rule)
        return
    await set_rule_active(rule, update.effective_user.id, context, active)
    await update.message.reply_text(f"✅ 红利机会监控规则 ID：{rule_id} 已{'开启' if active else '关闭'}。")


async def start_resume(message, context, user_id, rule):
    async def resume(work):
        await work.progress(f"正在为规则 {rule['id']} 计算恢复基线。")
        if not rule_is_current(rule):
            return "规则已变更或删除，恢复操作已跳过。"
        if await set_rule_active(rule, user_id, context, True, work):
            return f"✅ 规则 {rule['id']} 已恢复监控。使用 /oplist 查看。"
        return "规则已变更或删除，恢复操作已跳过。"
    await task_manager(context).submit(user_id, message, "恢复机会监控", resume)


async def set_rule_active(rule, user_id, context, active, work=None):
    if rule["is_active"] == active and active:
        return True
    if active:
        snapshot = await evaluate_opportunity(rule, context)
        if work:
            work.check()
        if not rule_is_current(rule):
            return False
        save_opportunity_snapshot(snapshot, critical=True)
        db_execute(
            """
            UPDATE opportunity_rules
            SET is_active = 1, revision = revision + 1, last_score = ?, last_level = ?,
                last_alert_score = NULL, last_alert_level = NULL, last_alert_at = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                snapshot.total_score,
                snapshot.level,
                datetime.now(SHANGHAI_TZ).isoformat(),
                rule["id"],
                user_id,
            ),
            swallow_errors=False,
        )
    else:
        db_execute(
            "UPDATE opportunity_rules SET is_active = 0, revision = revision + 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (datetime.now(SHANGHAI_TZ).isoformat(), rule["id"], user_id),
            swallow_errors=False,
        )

    return True


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
        db_execute(
            "UPDATE whitelist SET daily_briefing_enabled = 1 WHERE user_id = ?",
            (user_id,),
            swallow_errors=False,
        )
        await update.message.reply_text("✅ 已为您开启每日收盘前简报功能。")
    elif command == "off":
        db_execute(
            "UPDATE whitelist SET daily_briefing_enabled = 0 WHERE user_id = ?",
            (user_id,),
            swallow_errors=False,
        )
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
        await update.message.reply_text("命令格式错误。\n正确格式：/add_w <用户 ID>")


@admin_only
async def del_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        _, user_id_str = update.message.text.split()
        user_id = int(user_id_str)
        if user_id == ADMIN_USER_ID:
            await update.message.reply_text("❌ 不能将管理员从白名单中删除。")
            return
        remove_from_whitelist(user_id)
        task_manager(context).cancel(user_id)
        await update.message.reply_text(f"✅ 用户 {user_id} 已从白名单中移除。")
    except (ValueError, IndexError):
        await update.message.reply_text("命令格式错误。\n正确格式：/del_w <用户 ID>")


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
    args = tuple(context.args)
    await task_manager(context).submit(
        update.effective_user.id, update.message, "查询代理状态",
        lambda work: _proxy_status_command(update, context, args, work),
    )


async def _proxy_status_command(update, context, args, work=None):
    if args and args != ("refresh",):
        await update.message.reply_text("正确格式：/proxy_status [refresh]")
        return
    status = await check_proxy_balance_async(force=bool(args))
    await notify_proxy_health(context.bot)
    checked_at = status.checked_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M %z")
    checked_at = f"{checked_at[:-2]}:{checked_at[-2:]}"
    balance = "暂无" if status.balance is None else f"{status.balance:g}"
    threshold = (
        "未启用"
        if AKSHARE_PROXY_LOW_BALANCE_THRESHOLD <= 0
        else f"{AKSHARE_PROXY_LOW_BALANCE_THRESHOLD:g}"
    )
    active = proxy_patch_active()
    history_state = (
        "可用"
        if active and status.state == POSITIVE
        else "降级" if ENABLE_AKSHARE_PROXY_PATCH else "直连数据源"
    )
    lines = [
        "AKShare Proxy 状态",
        "",
        f"已配置：{'是' if ENABLE_AKSHARE_PROXY_PATCH else '否'}",
        f"补丁已启用：{'是' if active else '否'}",
        f"余额状态：{PROXY_STATE_LABELS.get(proxy_health_category(status), status.state)}",
        f"余额：{balance}",
        f"上次检查：{checked_at}",
        f"低余额阈值：{threshold}",
        "",
        f"ETF 复权历史状态：{history_state}",
    ]
    if ENABLE_AKSHARE_PROXY_PATCH and status.state != POSITIVE:
        retry_at = next_balance_retry_at(status)
        if retry_at is not None:
            retry = retry_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M %z")
            lines.append(f"下次余额检查：{retry[:-2]}:{retry[-2:]}")
    if status.state == POSITIVE and not active and ENABLE_AKSHARE_PROXY_PATCH:
        lines.extend(
            [
                "",
                "当前余额已恢复，但启动时未安装 Proxy 补丁。",
                "请重启 Bot 以安全启用。",
            ]
        )
    if work:
        work.check()
        return "\n".join(lines)
    await update.message.reply_text("\n".join(lines))


@admin_only
async def refresh_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data[KEY_HIST_CACHE] = {}
    context.bot_data[KEY_HIST_FAILURE_CACHE] = {}
    context.bot_data[KEY_CACHE_DATE] = None
    await update.message.reply_text("✅ 历史数据缓存已清空，下次检查时将重新获取。")
