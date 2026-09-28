"""Bounded, disposable provider processes; cancellation releases actual capacity."""

import asyncio
import pickle
import sys
from pathlib import Path
from weakref import WeakKeyDictionary

_slots = WeakKeyDictionary()


class ProviderCallError(RuntimeError):
    pass


async def run_provider_call(function, *args, timeout, proxy_active=False, **kwargs):
    loop = asyncio.get_running_loop()
    slots = _slots.setdefault(loop, asyncio.Semaphore(4))
    payload = pickle.dumps((function.__module__, function.__qualname__, args, kwargs, proxy_active))
    # Queue time is bounded too. The child is never given credentials in argv.
    async with asyncio.timeout(timeout):
        async with slots:
            startup = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, '-m', 'src.provider_worker',
                cwd=str(Path(__file__).resolve().parent.parent),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ))
            try:
                process = await asyncio.shield(startup)
            except asyncio.CancelledError:
                # Cancellation can arrive while asyncio is establishing pipes.
                # Wait for ownership of the child before killing and reaping it.
                process = await startup
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.communicate()
                raise
            communication = asyncio.create_task(process.communicate(payload))
            try:
                stdout, _ = await asyncio.shield(communication)
                if process.returncode != 0:
                    raise ProviderCallError('数据源工作进程异常退出')
                success, value = pickle.loads(stdout)
                if not success:
                    raise ProviderCallError(f'数据源调用失败: {value}')
                return value
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # Drain and reap before releasing the admission slot.
                await asyncio.shield(communication)
