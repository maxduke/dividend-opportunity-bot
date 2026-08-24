import asyncio


def test_akshare_call_timeout_returns_none(monkeypatch):
    from src import data_fetcher

    monkeypatch.setattr(data_fetcher, "AKSHARE_CALL_TIMEOUT_SECONDS", 0.01)

    def hangs():
        import time

        time.sleep(0.1)

    assert asyncio.run(data_fetcher._call_akshare(hangs)) is None
