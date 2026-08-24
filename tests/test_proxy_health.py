import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def reset_proxy_health(monkeypatch):
    from src import proxy_health

    proxy_health._cached_status = None
    proxy_health._cache_identity = None
    proxy_health._patch_active = False
    proxy_health.last_notified_proxy_health_state = None
    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", False)
    monkeypatch.setattr(proxy_health, "AKSHARE_PROXY_AUTH_IP", "127.0.0.1")
    monkeypatch.setattr(proxy_health, "AKSHARE_PROXY_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(proxy_health, "AKSHARE_PROXY_BALANCE_CACHE_MINUTES", 30)
    monkeypatch.setattr(proxy_health, "AKSHARE_PROXY_LOW_BALANCE_THRESHOLD", 0)
    monkeypatch.setattr(proxy_health, "ADMIN_USER_ID", 12345)
    yield
    proxy_health._cached_status = None
    proxy_health._cache_identity = None
    proxy_health._patch_active = False
    proxy_health.last_notified_proxy_health_state = None


def _response(payload, status_code=200):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def test_disabled_does_not_call_balance_api(monkeypatch):
    from src import proxy_health

    calls = []
    monkeypatch.setattr(proxy_health.requests, "get", lambda *args, **kwargs: calls.append(args))
    status = proxy_health.check_proxy_balance(force=True)
    assert status.state == proxy_health.DISABLED
    assert not calls


@pytest.mark.parametrize(
    ("payload", "expected_state", "expected_balance"),
    [
        ({"balance": 100}, "POSITIVE", 100.0),
        ({"balance": 0}, "NO_BALANCE_OR_INVALID", 0.0),
        ({"balance": -1}, "NO_BALANCE_OR_INVALID", -1.0),
        ({"balance": "100"}, "NO_BALANCE_OR_INVALID", None),
        ({"balance": float("nan")}, "NO_BALANCE_OR_INVALID", None),
        ({"balance": float("inf")}, "NO_BALANCE_OR_INVALID", None),
        ({}, "NO_BALANCE_OR_INVALID", None),
    ],
)
def test_balance_state_matrix(monkeypatch, payload, expected_state, expected_balance):
    from src import proxy_health

    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(proxy_health.requests, "get", lambda *args, **kwargs: _response(payload))
    status = proxy_health.check_proxy_balance(force=True)
    assert status.state == expected_state
    assert status.balance == expected_balance


def test_http_failure_and_timeout_are_unverified(monkeypatch):
    from src import proxy_health

    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(
        proxy_health.requests,
        "get",
        lambda *args, **kwargs: _response({}, status_code=503),
    )
    assert proxy_health.check_proxy_balance(force=True).state == proxy_health.UNVERIFIED

    def timeout(*args, **kwargs):
        raise requests.Timeout("secret-token in URL")

    monkeypatch.setattr(proxy_health.requests, "get", timeout)
    assert proxy_health.check_proxy_balance(force=True).state == proxy_health.UNVERIFIED


@pytest.mark.parametrize(
    ("status_code", "expected_state"),
    [
        (401, "NO_BALANCE_OR_INVALID"),
        (403, "NO_BALANCE_OR_INVALID"),
        (408, "UNVERIFIED"),
        (429, "UNVERIFIED"),
    ],
)
def test_http_4xx_state_matrix(monkeypatch, status_code, expected_state):
    from src import proxy_health

    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(
        proxy_health.requests,
        "get",
        lambda *args, **kwargs: _response({}, status_code=status_code),
    )
    assert proxy_health.check_proxy_balance(force=True).state == expected_state


def test_malformed_json_is_unverified(monkeypatch):
    from src import proxy_health

    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)

    def broken_json():
        raise ValueError("provider response is not JSON")

    monkeypatch.setattr(
        proxy_health.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(status_code=200, json=broken_json),
    )
    assert proxy_health.check_proxy_balance(force=True).state == proxy_health.UNVERIFIED


def test_balance_request_never_logs_secret(monkeypatch, caplog):
    from src import proxy_health

    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(
        proxy_health.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.RequestException("secret-token appears in URL")
        ),
    )
    status = proxy_health.check_proxy_balance(force=True)
    assert status.reason == "request_error"
    assert "secret-token" not in caplog.text
    assert "secret-token" not in repr(status)


def test_ttl_and_force(monkeypatch):
    from src import proxy_health

    calls = []
    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)

    def fetch(*args):
        calls.append(args)
        return proxy_health.ProxyBalanceStatus(proxy_health.POSITIVE, 1.0, args[-1])

    monkeypatch.setattr(proxy_health, "_fetch_balance", fetch)
    start = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    proxy_health.check_proxy_balance(force=True, now=start)
    proxy_health.check_proxy_balance(now=start + timedelta(minutes=29))
    assert len(calls) == 1
    proxy_health.check_proxy_balance(now=start + timedelta(minutes=30))
    proxy_health.check_proxy_balance(force=True, now=start + timedelta(minutes=30))
    assert len(calls) == 3


def test_async_balance_check_uses_same_cache(monkeypatch):
    from src import proxy_health

    monkeypatch.setattr(proxy_health, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(proxy_health.requests, "get", lambda *args, **kwargs: _response({"balance": 2}))
    status = asyncio.run(proxy_health.check_proxy_balance_async(force=True))
    assert status.state == proxy_health.POSITIVE
    assert proxy_health.get_cached_proxy_balance_status() == status


def test_notification_dedup_and_recovery():
    from src import proxy_health

    now = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    bot = SimpleNamespace(send_message=_AsyncRecorder())
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.NO_BALANCE_OR_INVALID, 0.0, now
    )
    assert asyncio.run(proxy_health.notify_proxy_health(bot, startup=True))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot, startup=True))
    assert len(bot.send_message.calls) == 1

    proxy_health.set_proxy_patch_active(True)
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.POSITIVE, 10.0, now + timedelta(minutes=30)
    )
    assert asyncio.run(proxy_health.notify_proxy_health(bot))
    assert len(bot.send_message.calls) == 2
    assert "secret-token" not in "\n".join(call["text"] for call in bot.send_message.calls)


def test_runtime_outage_warns_once_and_recovery_warns_once():
    from src import proxy_health

    now = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    bot = SimpleNamespace(send_message=_AsyncRecorder())
    proxy_health.set_proxy_patch_active(True)
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.POSITIVE, 10.0, now
    )
    asyncio.run(proxy_health.notify_proxy_health(bot, startup=True))

    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.NO_BALANCE_OR_INVALID, 0.0, now + timedelta(minutes=30)
    )
    assert asyncio.run(proxy_health.notify_proxy_health(bot))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot))

    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.POSITIVE, 20.0, now + timedelta(minutes=60)
    )
    assert asyncio.run(proxy_health.notify_proxy_health(bot))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot))
    assert len(bot.send_message.calls) == 2


def test_runtime_unverified_warns_once():
    from src import proxy_health

    now = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    bot = SimpleNamespace(send_message=_AsyncRecorder())
    proxy_health.set_proxy_patch_active(True)
    proxy_health.last_notified_proxy_health_state = proxy_health.POSITIVE
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.UNVERIFIED, None, now, "request_error"
    )

    assert asyncio.run(proxy_health.notify_proxy_health(bot))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot))
    assert len(bot.send_message.calls) == 1
    assert "运行时余额检查失败" in bot.send_message.calls[0]["text"]


def test_failed_notification_is_retried(caplog):
    from src import proxy_health

    now = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    recorder = _FailOnceRecorder()
    bot = SimpleNamespace(send_message=recorder)
    proxy_health.set_proxy_patch_active(True)
    proxy_health.last_notified_proxy_health_state = proxy_health.POSITIVE
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.NO_BALANCE_OR_INVALID, 0.0, now
    )

    assert not asyncio.run(proxy_health.notify_proxy_health(bot))
    assert proxy_health.last_notified_proxy_health_state == proxy_health.POSITIVE
    assert asyncio.run(proxy_health.notify_proxy_health(bot))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot))
    assert len(recorder.calls) == 2
    assert "secret-token" not in caplog.text


def test_low_balance_stays_usable_and_warns_once(monkeypatch):
    from src import proxy_health

    now = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    bot = SimpleNamespace(send_message=_AsyncRecorder())
    monkeypatch.setattr(proxy_health, "AKSHARE_PROXY_LOW_BALANCE_THRESHOLD", 100)
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.POSITIVE, 80.0, now
    )

    assert proxy_health.proxy_health_category() == proxy_health.LOW_BALANCE
    assert asyncio.run(proxy_health.notify_proxy_health(bot, startup=True))
    assert not asyncio.run(proxy_health.notify_proxy_health(bot))
    assert len(bot.send_message.calls) == 1


def test_recharge_to_low_positive_balance_still_sends_recovery(monkeypatch):
    from src import proxy_health

    now = datetime(2026, 8, 24, 12, tzinfo=SHANGHAI)
    bot = SimpleNamespace(send_message=_AsyncRecorder())
    monkeypatch.setattr(proxy_health, "AKSHARE_PROXY_LOW_BALANCE_THRESHOLD", 100)
    proxy_health.set_proxy_patch_active(True)
    proxy_health.last_notified_proxy_health_state = proxy_health.NO_BALANCE_OR_INVALID
    proxy_health._cached_status = proxy_health.ProxyBalanceStatus(
        proxy_health.POSITIVE, 80.0, now
    )

    assert asyncio.run(proxy_health.notify_proxy_health(bot))
    assert "已恢复" in bot.send_message.calls[0]["text"]


class _AsyncRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _FailOnceRecorder(_AsyncRecorder):
    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("temporary Telegram failure for secret-token")
