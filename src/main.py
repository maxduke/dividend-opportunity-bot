# -*- coding: utf-8 -*-

import logging
from datetime import time
from zoneinfo import ZoneInfo

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

from .config import (
    BRIEFING_TIMES_STR,
    CHECK_INTERVAL_SECONDS,
    ENABLE_INTRADAY_MONITOR,
    KEY_CACHE_DATE,
    KEY_HIST_CACHE,
    KEY_HIST_FAILURE_CACHE,
    KEY_NAME_CACHE,
    KEY_QUOTE_FAILURE_COUNTS,
    KEY_QUOTE_FAILURE_NOTIFIED,
    TELEGRAM_TOKEN,
    log_config,
    validate_config,
)
from .database import db_execute, db_init
from .provider_bootstrap import install_data_provider_patch
from .proxy_health import notify_proxy_health
from .user_tasks import cancel_callback, cancel_command, stop_user_tasks, task_command

logger = logging.getLogger(__name__)


async def post_init(application: Application):
    await application.bot.set_my_commands(
        [
            BotCommand("start", "开始使用机器人"),
            BotCommand("help", "获取帮助信息"),
            BotCommand("briefing", "开关每日简报"),
            BotCommand("addop", "添加红利机会监控"),
            BotCommand("delop", "删除机会监控: ID"),
            BotCommand("oplist", "查看机会监控"),
            BotCommand("opon", "开启机会监控: ID"),
            BotCommand("opoff", "关闭机会监控: ID"),
            BotCommand("opcheck", "查询机会摘要"),
            BotCommand("opthreshold", "修改告警阈值: ID 分数"),
            BotCommand("task", "查看后台任务进度"),
            BotCommand("cancel", "取消当前后台任务"),
            BotCommand("proxy_status", "查看 AKShare Proxy 状态"),
        ]
    )
    application.bot_data.update(
        {
            KEY_HIST_CACHE: {},
            KEY_HIST_FAILURE_CACHE: {},
            KEY_NAME_CACHE: {},
            KEY_QUOTE_FAILURE_COUNTS: {},
            KEY_QUOTE_FAILURE_NOTIFIED: {},
            KEY_CACHE_DATE: None,
        }
    )
    rules = db_execute(
        "SELECT asset_code, asset_name FROM opportunity_rules", fetchall=True
    ) or []
    for rule in rules:
        if rule["asset_code"] and rule["asset_name"]:
            application.bot_data[KEY_NAME_CACHE][rule["asset_code"]] = rule["asset_name"]
    await notify_proxy_health(application.bot, startup=True)
    logger.info("Bot application data 初始化完成。")


async def error_handler(update: object, context) -> None:
    logger.error("未捕获的异常: %s", context.error, exc_info=context.error)


def _command(name, callback):
    # Edited messages have no ``update.message``; editing must not re-run a command.
    return CommandHandler(name, callback, filters=filters.UpdateType.MESSAGE)


def _register_intraday_job(job_queue, job, enabled: bool = ENABLE_INTRADAY_MONITOR):
    if not enabled:
        logger.info("Intraday opportunity monitor disabled; repeating market job not registered.")
        return
    job_queue.run_repeating(job, interval=CHECK_INTERVAL_SECONDS, first=10)
    logger.info("Intraday opportunity monitor enabled; interval=%ss.", CHECK_INTERVAL_SECONDS)


def main():
    validate_config()
    log_config()
    install_data_provider_patch()
    db_init()

    # Provider-facing imports happen only after the optional patch is installed.
    from .handlers import (
        add_opportunity_rule_command,
        add_whitelist_command,
        briefing_command,
        enable_briefing_callback,
        check_opportunity_command,
        del_whitelist_command,
        delete_opportunity_rule_command,
        help_command,
        list_opportunity_rules_command,
        list_whitelist_command,
        proxy_status_command,
        refresh_cache_command,
        start_command,
        toggle_opportunity_rule_command,
        threshold_opportunity_command,
    )
    from .jobs import check_opportunity_job, daily_briefing_job
    from .rule_ui import rule_callback

    application = (
        Application.builder().token(TELEGRAM_TOKEN)
        .post_init(post_init).post_stop(stop_user_tasks).build()
    )
    application.add_error_handler(error_handler)
    application.add_handlers(
        [
            CallbackQueryHandler(enable_briefing_callback, pattern=r"^briefing_on:\d+$"),
            CallbackQueryHandler(rule_callback, pattern=r"^op:"),
            CallbackQueryHandler(cancel_callback, pattern=r"^task:"),
            _command("task", task_command),
            _command("cancel", cancel_command),
            _command("start", start_command),
            _command("help", help_command),
            _command("briefing", briefing_command),
            _command("addop", add_opportunity_rule_command),
            _command("delop", delete_opportunity_rule_command),
            _command("oplist", list_opportunity_rules_command),
            _command("opon", toggle_opportunity_rule_command),
            _command("opoff", toggle_opportunity_rule_command),
            _command("opcheck", check_opportunity_command),
            _command("opthreshold", threshold_opportunity_command),
            _command("add_w", add_whitelist_command),
            _command("del_w", del_whitelist_command),
            _command("list_w", list_whitelist_command),
            _command("proxy_status", proxy_status_command),
            _command("refresh", refresh_cache_command),
        ]
    )

    job_queue = application.job_queue
    _register_intraday_job(job_queue, check_opportunity_job)
    successful_times = []
    for time_str in (value.strip() for value in BRIEFING_TIMES_STR.split(",")):
        if not time_str:
            continue
        try:
            hour, minute = map(int, time_str.split(":"))
            briefing_time = time(hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
            job_queue.run_daily(
                daily_briefing_job,
                time=briefing_time,
                name=f"daily_briefing_{time_str}",
            )
            successful_times.append(time_str)
        except (ValueError, IndexError):
            logger.error("每日简报时间格式错误 ('%s')，应为 HH:MM。", time_str)
    if successful_times:
        logger.info(
            "已成功注册每日简报任务，将于每天 %s (上海时间) 执行。",
            ", ".join(successful_times),
        )
    application.run_polling()


if __name__ == "__main__":
    main()
