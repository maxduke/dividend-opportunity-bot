import sys
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_SPEC = spec_from_file_location(
    "verify_data_sources", Path(__file__).parents[1] / "scripts" / "verify_data_sources.py"
)
verify_data_sources = module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_data_sources)


def test_realtime_smoke_requires_valid_past_timestamp_but_not_today():
    now = datetime(2026, 8, 24, 14, 50, tzinfo=verify_data_sources.SHANGHAI_TZ)
    ok, detail = verify_data_sources._validate_realtime_quote(
        SimpleNamespace(price=1.2, timestamp="2026-08-21 15:00:00"), now
    )
    assert ok
    assert detail["price"] == 1.2
    assert "2026-08-21" in detail["timestamp"]


def test_realtime_smoke_rejects_missing_or_future_timestamp():
    now = datetime(2026, 8, 24, 14, 50, tzinfo=verify_data_sources.SHANGHAI_TZ)
    assert not verify_data_sources._validate_realtime_quote(
        SimpleNamespace(price=1.2, timestamp=None), now
    )[0]
    assert not verify_data_sources._validate_realtime_quote(
        SimpleNamespace(price=1.2, timestamp="2026-08-24 14:50:01"), now
    )[0]


def test_etf_smoke_requires_qfq_history(monkeypatch):
    responses = iter(
        [
            (True, {
                "rows": 300,
                "valid": 300,
                "price_basis": "unadjusted_fallback",
                "price_basis_asof": "2026-08-24",
                "latest_history_date": "2026-08-21",
            }),
            (True, {"price": 1.2, "timestamp": "2026-08-21T15:00:00+08:00"}),
        ]
    )
    monkeypatch.setattr(verify_data_sources, "_isolated_call", lambda *args, **kwargs: next(responses))
    assert verify_data_sources._check_asset("515180", timeout=1) == (False, True)


def test_proxy_etf_smoke_calls_qfq(monkeypatch):
    from src import provider_bootstrap

    calls = []

    def fetch_etf(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame({"收盘": [1.0] * 253})

    fake_requests = SimpleNamespace(
        _OriginalSession=SimpleNamespace(request=lambda *args, **kwargs: None)
    )
    monkeypatch.setattr(provider_bootstrap, "install_data_provider_patch", lambda: True)
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(fund_etf_hist_em=fetch_etf))
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    queued = []
    verify_data_sources._isolated_worker(
        SimpleNamespace(put=queued.append),
        "proxy_etf_history",
        ("515180", "20250101", "20260824"),
    )

    assert calls[0]["adjust"] == "qfq"
    assert queued == [(True, {
        "rows": 253,
        "valid_close": 253,
        "adjust": "qfq",
        "proxy_route": "PATCH",
        "proxy_auth_fetched": True,
    })]


def test_proxy_checks_require_balance_patch_and_qfq_route(monkeypatch, capsys):
    responses = iter(
        [
            (True, {
                "configured": True,
                "state": "POSITIVE",
                "balance": 12.0,
                "checked_at": "2026-08-24T20:15:00+08:00",
                "patch_active": True,
            }),
            (True, {"rows": 300, "valid_close": 300, "adjust": "qfq", "proxy_route": "PATCH"}),
        ]
    )
    monkeypatch.setattr(verify_data_sources, "_isolated_call", lambda *args, **kwargs: next(responses))

    assert verify_data_sources._check_proxy_interfaces("515180", timeout=1)
    output = capsys.readouterr().out
    assert "Proxy balance" in output
    assert "Proxy patch" in output
    assert "Proxy ETF history" in output
    assert "12.0" in output


def test_proxy_checks_fail_without_positive_balance_or_patch(monkeypatch, capsys):
    responses = iter(
        [
            (False, {
                "configured": True,
                "state": "NO_BALANCE_OR_INVALID",
                "balance": 0.0,
                "checked_at": "2026-08-24T20:15:00+08:00",
                "patch_active": False,
            }),
            (False, "proxy patch is disabled"),
        ]
    )
    monkeypatch.setattr(verify_data_sources, "_isolated_call", lambda *args, **kwargs: next(responses))
    assert not verify_data_sources._check_proxy_interfaces("515180", timeout=1)
    assert "FAIL: no positive balance" in capsys.readouterr().out
