import asyncio
import threading


def test_akshare_call_timeout_returns_none(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "AKSHARE_CALL_TIMEOUT_SECONDS", 0.01)

    def hangs():
        import time

        time.sleep(0.1)

    assert asyncio.run(data_fetcher._call_akshare(hangs)) is None


def test_akshare_timeout_keeps_abandoned_threads_bounded(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "AKSHARE_CALL_TIMEOUT_SECONDS", 0.02)
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def hangs():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait()
        with lock:
            active -= 1

    async def exercise():
        return await asyncio.gather(*(data_fetcher._call_akshare(hangs) for _ in range(8)))

    try:
        assert asyncio.run(exercise()) == [None] * 8
        assert maximum <= 4
    finally:
        release.set()
