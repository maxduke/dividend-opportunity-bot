import asyncio
from datetime import timedelta

import pandas as pd

from scripts.analyze_scoring import _fetch_adjusted_history, print_report, replay_scores


def test_adjusted_history_installs_proxy_patch_before_provider_call(monkeypatch):
    from src import data_fetcher, provider_bootstrap

    calls = []

    def install_patch():
        calls.append("patch")

    async def get_history_data(asset, days):
        calls.append((asset, days))
        frame = pd.DataFrame({"日期": ["2026-08-24"], "收盘": [100.0]})
        frame.attrs["price_basis"] = "qfq"
        return frame

    monkeypatch.setattr(provider_bootstrap, "install_data_provider_patch", install_patch)
    monkeypatch.setattr(data_fetcher, "get_history_data", get_history_data)

    result = asyncio.run(_fetch_adjusted_history("515180", 300))

    assert calls == ["patch", ("515180", 300)]
    assert result.attrs["price_basis"] == "qfq"


def test_replay_does_not_use_future_values_for_an_earlier_score():
    dates = pd.bdate_range("2024-01-02", periods=320)
    target = dates[270].date()
    prices = pd.DataFrame({"日期": dates, "收盘": [100 + i * 0.1 for i in range(len(dates))]})
    valuations = [
        {"valuation_date": dates[0].date(), "dividend_yield2": 4.0},
        {"valuation_date": target, "dividend_yield2": 5.0},
        {"valuation_date": (dates[271] + timedelta(days=1)).date(), "dividend_yield2": 99.0},
    ]
    bonds = [
        {"yield_date": dates[0].date(), "cn10y": 2.0, "source": "chinabond"},
        {"yield_date": target, "cn10y": 2.0, "source": "chinabond"},
        {"yield_date": (dates[271] + timedelta(days=1)).date(), "cn10y": -50.0, "source": "future"},
    ]

    changed_prices = prices.copy()
    changed_prices.loc[changed_prices["日期"] > pd.Timestamp(target), "收盘"] = 900.0
    changed_valuations = valuations[:-1] + [
        {"valuation_date": (dates[271] + timedelta(days=1)).date(), "dividend_yield2": -99.0}
    ]
    changed_bonds = bonds[:-1] + [
        {"yield_date": (dates[271] + timedelta(days=1)).date(), "cn10y": 50.0, "source": "future"}
    ]

    kwargs = {"minimum_observations": 999, "minimum_span_years": 99}
    before = replay_scores(prices, valuations, bonds, **kwargs)
    after = replay_scores(changed_prices, changed_valuations, changed_bonds, **kwargs)
    before_row = before.loc[before["date"] == target].iloc[0]
    after_row = after.loc[after["date"] == target].iloc[0]

    for column in (
        "dividend_yield_score",
        "spread_score",
        "ma200_score",
        "drawdown_score",
        "rsi_score",
        "total_score",
        "level",
        "valuation_date",
        "cn10y_date",
    ):
        assert before_row[column] == after_row[column]

    # The later close is used only for evaluation, after the target score exists.
    assert before_row["forward_21d"] != after_row["forward_21d"]

    print_report(before)
