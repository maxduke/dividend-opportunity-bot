import asyncio
import os

import pytest

from src import data_fetcher
from src.provider_calls import ProviderCallError
from tests.provider_stub import echo, fail, hang


def assert_reaped(marker):
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_reaps_workers_and_healthy_fallback_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, 'proxy_patch_active', lambda: False)
    markers = [tmp_path / str(index) for index in range(4)]

    async def exercise():
        assert await asyncio.gather(*(
            data_fetcher._call_akshare(hang, str(marker), timeout_seconds=1)
            for marker in markers
        )) == [None] * 4
        for marker in markers:
            assert_reaped(marker)
        assert await data_fetcher._call_akshare(echo, 'healthy', timeout_seconds=2) == 'healthy'
    asyncio.run(exercise())


def test_cancel_reaps_provider_before_returning(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, 'proxy_patch_active', lambda: False)
    marker = tmp_path / 'pid'

    async def exercise():
        task = asyncio.create_task(data_fetcher._call_akshare(hang, str(marker)))
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_reaped(marker)
    asyncio.run(exercise())


def test_worker_roundtrips_dataframe_and_attributes(monkeypatch):
    import pandas as pd
    monkeypatch.setattr(data_fetcher, 'proxy_patch_active', lambda: False)
    frame = pd.DataFrame({'收盘': [1., 2.]}, index=pd.date_range('2026-08-20', periods=2))
    frame.attrs['price_basis'] = 'qfq'
    result = asyncio.run(data_fetcher._call_akshare(echo, frame, timeout_seconds=5))
    pd.testing.assert_frame_equal(result, frame)
    assert result.attrs == frame.attrs


def test_worker_error_does_not_expose_provider_url(monkeypatch):
    monkeypatch.setattr(data_fetcher, 'proxy_patch_active', lambda: False)
    with pytest.raises(ProviderCallError, match='ConnectionError') as caught:
        asyncio.run(data_fetcher._call_akshare(fail, timeout_seconds=2))
    assert 'secret' not in str(caught.value)


def test_worker_installs_verified_patch_before_loading_provider(monkeypatch):
    import io
    import pickle
    from types import SimpleNamespace
    from src import provider_worker, provider_bootstrap
    order = []
    monkeypatch.setattr(provider_bootstrap, 'install_verified_proxy_patch', lambda: order.append('patch'))
    def load(name):
        order.append('import')
        return SimpleNamespace(call=lambda: 'ok')
    monkeypatch.setattr(provider_worker.importlib, 'import_module', load)
    output = io.BytesIO()
    monkeypatch.setattr(provider_worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(
        pickle.dumps(('provider', 'call', (), {}, True)))))
    monkeypatch.setattr(provider_worker.sys, 'stdout', SimpleNamespace(buffer=output))
    provider_worker.main()
    assert order == ['patch', 'import']
    assert pickle.loads(output.getvalue()) == (True, 'ok')


def test_queue_is_bounded_and_cancelling_waiter_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, 'proxy_patch_active', lambda: False)
    markers = [tmp_path / str(index) for index in range(5)]
    async def exercise():
        active = [asyncio.create_task(data_fetcher._call_akshare(hang, str(marker), timeout_seconds=10))
                  for marker in markers[:4]]
        async with asyncio.timeout(3):
            while not all(marker.exists() for marker in markers[:4]):
                await asyncio.sleep(.01)
        queued = asyncio.create_task(data_fetcher._call_akshare(hang, str(markers[4]), timeout_seconds=10))
        await asyncio.sleep(.1)
        assert not markers[4].exists()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        for marker in markers[:4]:
            assert_reaped(marker)
        assert not markers[4].exists()
    asyncio.run(exercise())


def test_worker_env_strips_sensitive_tokens(monkeypatch):
    from src.provider_calls import _worker_env

    monkeypatch.setenv("TELEGRAM_TOKEN", "bot-secret")
    monkeypatch.setenv("AKSHARE_PROXY_AUTH_TOKEN", "proxy-secret")

    non_proxy = _worker_env(proxy_active=False)
    assert "TELEGRAM_TOKEN" not in non_proxy
    assert "AKSHARE_PROXY_AUTH_TOKEN" not in non_proxy

    with_proxy = _worker_env(proxy_active=True)
    assert "TELEGRAM_TOKEN" not in with_proxy
    assert with_proxy["AKSHARE_PROXY_AUTH_TOKEN"] == "proxy-secret"


def test_call_akshare_enables_proxy_only_for_eastmoney_functions(monkeypatch):
    import akshare as ak

    recorded = []

    async def fake_run_provider_call(function, *args, timeout, proxy_active=False, **kwargs):
        recorded.append((function.__name__, proxy_active))
        return "ok"

    monkeypatch.setattr(data_fetcher, "proxy_patch_active", lambda: True)
    monkeypatch.setattr(data_fetcher, "run_provider_call", fake_run_provider_call)

    async def exercise():
        await data_fetcher._call_akshare(ak.fund_etf_hist_em)
        await data_fetcher._call_akshare(ak.stock_zh_a_hist)
        await data_fetcher._call_akshare(ak.stock_individual_info_em)
        await data_fetcher._call_akshare(ak.stock_zh_a_minute)
        await data_fetcher._call_akshare(ak.stock_zh_index_value_csindex)
        await data_fetcher._call_akshare(ak.bond_china_yield)

    asyncio.run(exercise())

    assert recorded == [
        ("fund_etf_hist_em", True),
        ("stock_zh_a_hist", True),
        ("stock_individual_info_em", True),
        ("stock_zh_a_minute", False),
        ("stock_zh_index_value_csindex", False),
        ("bond_china_yield", False),
    ]
