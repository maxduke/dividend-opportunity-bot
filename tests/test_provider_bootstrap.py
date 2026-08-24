from datetime import UTC, datetime

import pytest


@pytest.mark.parametrize(
    "state",
    ["NO_BALANCE_OR_INVALID", "UNVERIFIED"],
)
def test_enabled_proxy_skips_patch_without_verified_positive_balance(
    monkeypatch, state
):
    from src import provider_bootstrap
    from src.proxy_health import ProxyBalanceStatus

    monkeypatch.setattr(provider_bootstrap, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_IP", "101.201.173.125")
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(provider_bootstrap, "_installed", False)
    monkeypatch.setattr(
        provider_bootstrap.proxy_health,
        "check_proxy_balance",
        lambda force: ProxyBalanceStatus(
            state, 0.0 if state == "NO_BALANCE_OR_INVALID" else None,
            datetime.now(UTC),
        ),
    )

    assert provider_bootstrap.install_data_provider_patch() is False
    assert provider_bootstrap.proxy_health.proxy_patch_active() is False


def test_proxy_patch_is_disabled_by_default(monkeypatch):
    from src import provider_bootstrap

    monkeypatch.setattr(provider_bootstrap, "ENABLE_AKSHARE_PROXY_PATCH", False)
    monkeypatch.setattr(provider_bootstrap, "_installed", False)
    assert provider_bootstrap.install_data_provider_patch() is False


def test_enabled_proxy_patch_requires_credentials(monkeypatch):
    from src import provider_bootstrap

    monkeypatch.setattr(provider_bootstrap, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_IP", "")
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_TOKEN", "")
    monkeypatch.setattr(provider_bootstrap, "_installed", False)
    with pytest.raises(RuntimeError, match="AUTH_IP/TOKEN"):
        provider_bootstrap.install_data_provider_patch()


def test_enabled_proxy_patch_forwards_documented_options(monkeypatch):
    from src import provider_bootstrap
    from src.proxy_health import POSITIVE, ProxyBalanceStatus

    calls = []

    class FakePatch:
        @staticmethod
        def install_patch(*args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(provider_bootstrap, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_IP", "101.201.173.125")
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_RETRY", 30)
    monkeypatch.setattr(
        provider_bootstrap,
        "AKSHARE_PROXY_HOOK_DOMAINS",
        "fund.eastmoney.com,push2.eastmoney.com",
    )
    monkeypatch.setattr(provider_bootstrap, "_installed", False)
    monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", FakePatch)
    monkeypatch.setattr(
        provider_bootstrap.proxy_health,
        "_fetch_balance",
        lambda auth_ip, auth_token, checked_at: ProxyBalanceStatus(
            POSITIVE, 100.0, checked_at
        ),
    )
    assert provider_bootstrap.install_data_provider_patch() is True
    assert calls == [
        (
            ("101.201.173.125",),
            {
                "auth_token": "test-token",
                "retry": 30,
                "hook_domains": ["fund.eastmoney.com", "push2.eastmoney.com"],
                "fast": False,
            },
        )
    ]
