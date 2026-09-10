"""Bounded, event-loop-owned user operations, independent of Telegram update dispatch."""

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .database import is_whitelisted

logger = logging.getLogger(__name__)
TASK_MANAGER_KEY = 'user_task_manager'


class AccessRevoked(Exception):
    pass


@dataclass
class UserTask:
    user_id: int
    title: str
    token: str = field(default_factory=lambda: uuid4().hex[:16])
    state: str = '排队中'
    detail: str = ''
    message: object = None
    task: asyncio.Task | None = None
    cancel_requested: bool = False
    result_markup: object = None

    def text(self):
        return f'{self.title} · {self.state}\n{self.detail}'.rstrip()

    def keyboard(self):
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            '取消任务', callback_data=f'task:{self.user_id}:{self.token}'
        )]])

    def check(self):
        if self.cancel_requested:
            raise asyncio.CancelledError
        if not is_whitelisted(self.user_id):
            raise AccessRevoked

    async def progress(self, detail):
        self.check()
        self.detail = detail
        await self._notify(self.keyboard())
        self.check()

    async def _notify(self, markup=None):
        if self.message is None:
            return
        try:
            await asyncio.wait_for(
                self.message.edit_text(self.text(), reply_markup=markup), timeout=5,
            )
        except Exception:
            logger.warning('任务状态消息更新失败 user_id=%s', self.user_id)


class UserTaskManager:
    """All access occurs on the application's event loop; no await during admission checks."""

    def __init__(self, concurrency=2, capacity=20):
        self.slots = asyncio.Semaphore(concurrency)
        self.capacity = capacity
        self.active = {}
        self.recent = OrderedDict()
        self.closing = False

    async def submit(self, user_id, message, title, operation):
        if self.closing:
            await message.reply_text('服务正在关闭，暂不接受新任务。')
            return None
        existing = self.active.get(user_id)
        if existing is not None:
            await message.reply_text('您已有一个耗时任务，请等待完成或先取消。\n' + existing.text(),
                                     reply_markup=existing.keyboard())
            return None
        if len(self.active) >= self.capacity:
            await message.reply_text('当前任务队列已满，请稍后重试。')
            return None
        work = UserTask(user_id, title)
        self.active[user_id] = work
        try:
            work.message = await message.reply_text(work.text(), reply_markup=work.keyboard())
            if self.closing or work.cancel_requested:
                if self.active.get(user_id) is work:
                    self.active.pop(user_id)
                return None
            # Application.create_task would make Application.stop wait before our
            # post_stop cancellation hook. Own and drain these tasks explicitly.
            work.task = asyncio.create_task(self._run(work, operation), name=f'user-operation-{work.token}')
            # Enter the runner's try/finally before a later update can cancel it.
            await asyncio.sleep(0)
            return work
        except BaseException:
            if self.active.get(user_id) is work:
                self.active.pop(user_id)
            if work.task:
                work.task.cancel()
            raise

    async def _run(self, work, operation):
        try:
            async with self.slots:
                work.check()
                work.state = '运行中'
                await work.progress('正在处理，可使用 /task 查看进度。')
                result = await operation(work)
                work.check()
                work.state = '已结束'
                work.detail = result if result is not None else work.detail or '请查看操作结果。'
        except asyncio.CancelledError:
            work.state = '已取消'
            work.detail = '已完成的结果会保留，未完成部分不再继续。'
            work.result_markup = None
        except AccessRevoked:
            work.state = '已停止'
            work.detail = '白名单权限已撤销。'
        except Exception:
            logger.exception('后台任务失败 user_id=%s title=%s', work.user_id, work.title)
            work.state = '失败'
            work.detail = '请稍后重试。'
        finally:
            # Keep admission occupied until all operation cleanup has finished.
            try:
                if not self.closing and work.state != '已停止':
                    await work._notify(work.result_markup)
            finally:
                if self.active.get(work.user_id) is work:
                    self.active.pop(work.user_id)
                self.recent[work.user_id] = work
                self.recent.move_to_end(work.user_id)
                while len(self.recent) > 100:
                    self.recent.popitem(last=False)

    def cancel(self, user_id, token=None):
        work = self.active.get(user_id)
        if work is None or (token is not None and token != work.token):
            return False
        if work.state not in {'排队中', '运行中', '正在取消'}:
            return False
        if not work.cancel_requested:
            work.cancel_requested = True
            work.state = '正在取消'
            if work.task:
                work.task.cancel()
        return True

    async def shutdown(self):
        self.closing = True
        tasks = []
        for user_id, work in list(self.active.items()):
            self.cancel(user_id)
            if work.task:
                tasks.append(work.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()


def task_manager(context):
    return context.bot_data.setdefault(TASK_MANAGER_KEY, UserTaskManager())


async def task_command(update, context):
    user_id = update.effective_user.id
    if not is_whitelisted(user_id):
        await update.message.reply_text('抱歉，您没有权限使用此机器人。')
        return
    manager = task_manager(context)
    work = manager.active.get(user_id) or manager.recent.get(user_id)
    if work is None:
        await update.message.reply_text('当前没有任务。服务重启后不保留任务记录。')
    else:
        await update.message.reply_text(work.text(), reply_markup=work.keyboard() if user_id in manager.active else None)


async def cancel_command(update, context):
    user_id = update.effective_user.id
    if not is_whitelisted(user_id):
        await update.message.reply_text('抱歉，您没有权限使用此机器人。')
        return
    cancelled = task_manager(context).cancel(user_id)
    await update.message.reply_text('已请求取消任务，已完成的结果会保留。' if cancelled else '当前没有可取消的任务。')


async def cancel_callback(update, context):
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        _, owner, token = query.data.split(':')
    except (AttributeError, ValueError):
        await query.answer('取消按钮无效。', show_alert=True)
        return
    if owner != str(user_id) or not is_whitelisted(user_id):
        await query.answer('此按钮仅限原用户使用，且需要白名单权限。', show_alert=True)
        return
    cancelled = task_manager(context).cancel(user_id, token)
    await query.answer('已请求取消任务。' if cancelled else '该任务已结束，旧按钮不会取消新任务。', show_alert=True)


async def stop_user_tasks(application):
    manager = application.bot_data.get(TASK_MANAGER_KEY)
    if manager is not None:
        await manager.shutdown()
