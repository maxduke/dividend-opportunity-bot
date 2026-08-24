import asyncio
from datetime import date, timedelta

import pandas as pd

from scripts.analyze_scoring import (
    _fetch_adjusted_history,
    build_parser,
    fetch_adjusted_history,
    print_report,
    replay_scores,
)


def test_adjusted_history_installs_proxy_patch_before_provider_call(monkeypatch):
    from src import data_fetcher, provider_bootstrap

    calls = []

    def install_patch():
        calls.append("patch")

    async def get_history_data(asset, days, *, price_adjust):
        calls.append((asset, days, price_adjust))
        frame = pd.DataFrame({"日期": ["2026-08-24"], "收盘": [100.0]})
        frame.attrs["price_basis"] = price_adjust
        return frame

    monkeypatch.setattr(provider_bootstrap, "install_data_provider_patch", install_patch)
    monkeypatch.setattr(data_fetcher, "get_history_data", get_history_data)

    result = asyncio.run(_fetch_adjusted_history("515180", 300))

    assert calls == ["patch", ("515180", 300, "hfq")]
    assert result.attrs["price_basis"] == "hfq"


def test_replay_warmup_makes_long_term_indicators_available_on_first_requested_date():
    dates = pd.bdate_range("2024-01-02", periods=320)
    start = dates[252].date()
    prices = pd.DataFrame({"日期": dates, "收盘": [100 + i * 0.1 for i in range(len(dates))]})
    valuations = [{"valuation_date": dates[0].date(), "dividend_yield2": 4.0}]
    bonds = [{"yield_date": dates[0].date(), "cn10y": 2.0}]

    result = replay_scores(
        prices,
        valuations,
        bonds,
        start=start,
        minimum_observations=999,
        minimum_span_years=99,
    )

    first = result.iloc[0]
    assert first["date"] == start
    assert pd.notna(first["ma200_deviation"])
    assert pd.notna(first["drawdown_52w"])


def test_replay_fetch_window_includes_full_warmup(monkeypatch):
    from scripts import analyze_scoring

    requested_start = date(2026, 8, 24)
    captured = {}

    async def fetch(asset, days, price_adjust):
        captured.update(asset=asset, days=days, price_adjust=price_adjust)
        frame = pd.DataFrame({"日期": [requested_start], "收盘": [100.0]})
        frame.attrs["price_basis"] = price_adjust
        return frame

    monkeypatch.setattr(analyze_scoring, "_fetch_adjusted_history", fetch)
    fetch_adjusted_history("515180", requested_start, None, None, "hfq")

    elapsed = (analyze_scoring.datetime.now(analyze_scoring.SHANGHAI_TZ).date() - requested_start).days
    assert captured == {
        "asset": "515180",
        "days": max(analyze_scoring.TECHNICAL_HISTORY_DAYS, elapsed + analyze_scoring.TECHNICAL_HISTORY_DAYS + 1),
        "price_adjust": "hfq",
    }


def test_replay_price_adjustment_cli_defaults_to_hfq_and_allows_qfq_diagnostic(capsys):
    parser = build_parser()
    assert parser.parse_args(["--asset", "515180", "--benchmark", "000922"]).price_adjust == "hfq"
    assert parser.parse_args(
        ["--asset", "515180", "--benchmark", "000922", "--price-adjust", "qfq"]
    ).price_adjust == "qfq"

    result = pd.DataFrame(
        {
            "date": [date(2026, 8, 24)],
            "score": [50.0],
            "valuation_score": [0.0],
            "long_term_score": [0.0],
            "tactical_score": [0.0],
            "level": ["WATCH"],
            "scoring_mode": ["ABSOLUTE_FALLBACK"],
            **{f"forward_{horizon}d": [float("nan")] for horizon in (21, 63, 126, 252)},
            **{column: [0.0] for column in (
                "dividend_yield_score",
                "spread_score",
                "ma200_score",
                "drawdown_score",
                "rsi_score",
            )},
        }
    )
    print_report(result, "qfq")
    output = capsys.readouterr().out
    assert "Replay technical price basis: QFQ" in output
    assert "WARNING: qfq historical prices are restated after corporate actions" in output
    assert "PERCENTILE" in output
    assert "MIXED" in output
    assert "ABSOLUTE_FALLBACK" in output
    assert "NONE" in output


def test_replay_does_not_use_future_values_for_an_earlier_score():
    dates = pd.bdate_range("2024-01-02", periods=320)
    target = dates[270].date()
    prices = pd.DataFrame({"日期": dates, "收盘": [100 + i * 0.1 for i in range(len(dates))]})
    prices.attrs["price_basis"] = "hfq"
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
