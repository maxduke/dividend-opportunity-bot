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
    proxy_cache = SimpleNamespace(data=None)

    def fetch_etf(**kwargs):
        calls.append(kwargs)
        proxy_cache.data = {"authenticated": True}
        return pd.DataFrame({"收盘": [1.0] * 253})

    fake_requests = SimpleNamespace(
        _OriginalSession=SimpleNamespace(request=lambda *args, **kwargs: None)
    )
    monkeypatch.setattr(provider_bootstrap, "install_data_provider_patch", lambda: True)
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(fund_etf_hist_em=fetch_etf))
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", SimpleNamespace(_cache=proxy_cache))
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
        "proxy_auth_fetched": True,
    })]
