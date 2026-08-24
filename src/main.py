# -*- coding: utf-8 -*-

import logging
from collections import OrderedDict
from datetime import time

import pytz
from telegram import BotCommand
from telegram.ext import Application, CommandHandler

from .config import (
    ADMIN_USER_ID,
    ENABLE_AKSHARE_PROXY_PATCH,
    BRIEFING_TIMES_STR,
    CHECK_INTERVAL_SECONDS,
    ENABLE_DAILY_BRIEFING,
    KEY_CACHE_DATE,
    KEY_FAILURE_COUNT,
    KEY_FAILURE_SENT,
    KEY_HIST_CACHE,
    KEY_NAME_CACHE,
    TELEGRAM_TOKEN,
    log_config,
    validate_config,
)
from .provider_bootstrap import install_data_provider_patch
from .database import db_execute, db_init

logger = logging.getLogger(__name__)


async def post_init(application: Application):
    """在机器人启动后设置自定义命令并初始化bot_data。"""
    commands = [
        BotCommand("start", "开始使用机器人"),
        BotCommand("help", "获取帮助信息"),
        BotCommand("check", "立即查询当前RSI"),
        BotCommand("briefing", "开关每日简报"),
        BotCommand("add", "添加监控: CODE min max"),
        BotCommand("del", "删除监控: ID"),
        BotCommand("list", "查看我的监控"),
        BotCommand("on", "开启监控: ID"),
        BotCommand("off", "关闭监控: ID"),
        BotCommand("addop", "添加红利机会监控"),
        BotCommand("delop", "删除机会监控: ID"),
        BotCommand("oplist", "查看机会监控"),
        BotCommand("opon", "开启机会监控: ID"),
        BotCommand("opoff", "关闭机会监控: ID"),
        BotCommand("opcheck", "查询机会分数"),
    ]
    await application.bot.set_my_commands(commands)

    bot_data = application.bot_data
    bot_data[KEY_HIST_CACHE] = {}
    bot_data[KEY_NAME_CACHE] = OrderedDict()
    bot_data[KEY_FAILURE_COUNT] = 0
    bot_data[KEY_FAILURE_SENT] = False
    bot_data[KEY_CACHE_DATE] = None

    # 预加载缓存
    all_rules = db_execute("SELECT asset_code, asset_name FROM rules", fetchall=True)
    if all_rules:
        for rule in all_rules:
            if rule['asset_code'] and rule['asset_name']:
                bot_data[KEY_NAME_CACHE][rule['asset_code']] = rule['asset_name']
        logger.info(f"从数据库预加载了 {len(bot_data[KEY_NAME_CACHE])} 个资产名称到缓存。")
    logger.info("Bot application data 初始化完成。")
    if (
        ENABLE_AKSHARE_PROXY_PATCH
        and not bot_data.get("akshare_proxy_active")
        and ADMIN_USER_ID
    ):
        try:
            await application.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    "⚠️ AKShare 付费代理未启用：余额或代理服务当前无法确认。\n"
                    "本次进程将使用直连/新浪 fallback，请检查网络或余额后重启。"
                ),
            )
        except Exception as exc:
            logger.warning("发送代理未启用管理员通知失败: %s", exc)


async def error_handler(update: object, context) -> None:
    """记录所有未被捕获的异常。"""
    logger.error(f"未捕获的异常: {context.error}", exc_info=False)


def main():
    """主函数，用于启动机器人。"""
    validate_config()
    log_config()
    proxy_active = install_data_provider_patch()
    db_init()

    # Import provider-facing modules only after the optional patch is installed.
    from .handlers import (
        add_rule_command,
        add_whitelist_command,
        add_opportunity_rule_command,
        briefing_command,
        check_opportunity_command,
        check_rsi_command,
        del_whitelist_command,
        delete_rule_command,
        delete_opportunity_rule_command,
        help_command,
        list_rules_command,
        list_opportunity_rules_command,
        list_whitelist_command,
        refresh_cache_command,
        start_command,
        toggle_rule_status_command,
        toggle_opportunity_rule_command,
    )
    from .jobs import check_rules_job, daily_briefing_job

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.bot_data["akshare_proxy_active"] = proxy_active
    application.add_error_handler(error_handler)

    handlers = [
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        CommandHandler("check", check_rsi_command),
        CommandHandler("briefing", briefing_command),
        CommandHandler("add", add_rule_command),
        CommandHandler("list", list_rules_command),
        CommandHandler("del", delete_rule_command),
        CommandHandler("on", toggle_rule_status_command),
        CommandHandler("off", toggle_rule_status_command),
        CommandHandler("addop", add_opportunity_rule_command),
        CommandHandler("delop", delete_opportunity_rule_command),
        CommandHandler("oplist", list_opportunity_rules_command),
        CommandHandler("opon", toggle_opportunity_rule_command),
        CommandHandler("opoff", toggle_opportunity_rule_command),
        CommandHandler("opcheck", check_opportunity_command),
        CommandHandler("add_w", add_whitelist_command),
        CommandHandler("del_w", del_whitelist_command),
        CommandHandler("list_w", list_whitelist_command),
        CommandHandler("refresh", refresh_cache_command),
    ]
    application.add_handlers(handlers)

    job_queue = application.job_queue
    job_queue.run_repeating(check_rules_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    if ENABLE_DAILY_BRIEFING:
        briefing_times = [t.strip() for t in BRIEFING_TIMES_STR.split(',') if t.strip()]
        successful_times = []
        for time_str in briefing_times:
            try:
                hour, minute = map(int, time_str.split(':'))
                tz_shanghai = pytz.timezone('Asia/Shanghai')
                briefing_time = time(hour, minute, tzinfo=tz_shanghai)
                job_queue.run_daily(daily_briefing_job, time=briefing_time, name=f"daily_briefing_{time_str}")
                successful_times.append(time_str)
            except (ValueError, IndexError):
                logger.error(f"每日简报时间格式错误 ('{time_str}')，应为 HH:MM 格式。该时间点的任务未开启。")
        if successful_times:
            logger.info(f"已成功注册每日简报任务，将于每天 {', '.join(successful_times)} (上海时间) 执行。")

    logger.info("机器人正在启动...")
    application.run_polling()


if __name__ == '__main__':
    main()
