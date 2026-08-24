from types import SimpleNamespace

import pytest


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
        provider_bootstrap.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200, json=lambda: {"balance": 1}
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


def test_proxy_patch_stays_disabled_when_balance_cannot_be_verified(monkeypatch):
    from src import provider_bootstrap

    monkeypatch.setattr(provider_bootstrap, "ENABLE_AKSHARE_PROXY_PATCH", True)
    monkeypatch.setattr(provider_bootstrap, "AKSHARE_PROXY_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(provider_bootstrap, "_installed", False)
    monkeypatch.setattr(
        provider_bootstrap.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200, json=lambda: {"balance": 0}
        ),
    )

    assert provider_bootstrap.install_data_provider_patch() is False
