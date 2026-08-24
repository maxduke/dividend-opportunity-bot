import asyncio
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from src.opportunity import (
    OpportunitySnapshot,
    _history_maturity,
    _history_spreads,
    evaluate_opportunity,
    format_opportunity_alert,
    format_opportunity_detail,
    record_rule_alert,
    record_rule_evaluation,
    save_opportunity_snapshot,
    should_send_opportunity_alert,
)


def _rule(**changes):
    values = {
        "id": 1,
        "user_id": 9,
        "asset_code": "510300",
        "asset_name": "红利ETF",
        "benchmark_code": "000922",
        "benchmark_name": "中证红利",
        "min_score": 60,
        "last_score": None,
        "last_level": None,
        "last_alert_at": None,
    }
    values.update(changes)
    return values


def _snapshot(score, level):
    return OpportunitySnapshot(
        rule_id=1,
        asset_code="510300",
        asset_name="红利ETF",
        benchmark_code="000922",
        benchmark_name="中证红利",
        snapshot_at="2026-08-13T10:00:00+08:00",
        total_score=score,
        level=level,
    )


def test_alert_threshold_and_level_upgrade_logic():
    assert should_send_opportunity_alert(_rule(last_score=55, last_level="WATCH"), _snapshot(62, "MODERATE"))[0]
    assert not should_send_opportunity_alert(_rule(last_score=62, last_level="MODERATE"), _snapshot(65, "MODERATE"))[0]
    assert should_send_opportunity_alert(_rule(last_score=65, last_level="MODERATE"), _snapshot(77, "STRONG"))[0]
    assert not should_send_opportunity_alert(_rule(last_score=77, last_level="STRONG"), _snapshot(79, "STRONG"))[0]
    assert should_send_opportunity_alert(_rule(last_score=77, last_level="STRONG"), _snapshot(86, "RARE"))[0]


def test_cooldown_blocks_normal_crossing_but_upgrade_overrides():
    recent = "2026-08-13T11:00:00+08:00"
    rule = _rule(last_score=55, last_level="WATCH", last_alert_at=recent)
    now = datetime.fromisoformat("2026-08-13T12:00:00+08:00")
    assert not should_send_opportunity_alert(rule, _snapshot(62, "WATCH"), now=now)[0]
    assert should_send_opportunity_alert(rule, _snapshot(77, "STRONG"), now=now)[0]


def test_missing_valuation_cannot_create_strong_level():
    from src.metrics import classify_opportunity_level

    assert classify_opportunity_level(100, valuation_available=False) == "WATCH"


def test_alert_detail_contains_trigger_reason():
    assert "分数跨过该规则告警阈值" in format_opportunity_detail(
        _snapshot(62, "MODERATE"), alert_reason="threshold-crossing"
    )


def test_compact_alert_omits_pe_and_full_notes_but_detail_shows_dates():
    snapshot = _snapshot(78, "STRONG")
    snapshot.pe1 = 8.1
    snapshot.pe2 = 7.9
    snapshot.dividend_yield_used = 5.42
    snapshot.technical_price_date = "2026-08-24"
    snapshot.technical_price_basis = "qfq_realtime"
    snapshot.valuation_date = "2026-08-23"
    snapshot.cn10y_date = "2026-08-23"
    snapshot.cn10y_source = "chinabond"
    snapshot.data_notes = ["full audit note"]

    alert = format_opportunity_alert(snapshot, "level-upgrade")
    detail = format_opportunity_detail(snapshot)

    assert "PE1" not in alert and "PE2" not in alert and "full audit note" not in alert
    assert "Use /opcheck 1" in alert
    assert "Technical price: 2026-08-24" in detail
    assert "qfq_realtime" in detail and "CSI valuation: 2026-08-23" in detail


@pytest.mark.parametrize("write", [record_rule_evaluation, record_rule_alert])
def test_critical_rule_state_write_propagates_sqlite_failure(monkeypatch, write):
    def fail(*args, **kwargs):
        assert kwargs["swallow_errors"] is False
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr("src.opportunity.db_execute", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        write(1, _snapshot(78, "STRONG"))


def test_alert_snapshot_is_a_critical_write(monkeypatch):
    def fail(*args, **kwargs):
        assert kwargs["swallow_errors"] is False
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr("src.opportunity.db_execute", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        save_opportunity_snapshot(_snapshot(78, "STRONG"), alert_sent=True)


def test_evaluate_opportunity_is_deterministic_with_supplied_data(monkeypatch):
    dates = pd.date_range("2025-08-01", periods=300, freq="D")
    history = pd.DataFrame({"收盘": [100 + i * 0.01 for i in range(300)]}, index=dates)
    rule = _rule()
    valuation = {
        "valuation_date": "2026-08-13",
        "benchmark_name": "中证红利",
        "pe1": 8,
        "pe2": 7,
        "dividend_yield1": 5,
        "dividend_yield2": 5.2,
    }
    bond = {"yield_date": "2026-08-13", "cn10y": 1.8, "source": "chinabond"}
    context = SimpleNamespace(bot_data={})
    monkeypatch.setattr("src.opportunity.get_cached_valuation", AsyncMock(return_value=valuation))
    monkeypatch.setattr("src.opportunity.get_cached_cn10y", AsyncMock(return_value=bond))
    monkeypatch.setattr("src.opportunity.get_valuation_history", lambda *args: [])
    monkeypatch.setattr("src.opportunity.get_bond_history", lambda *args, **kwargs: [bond])
    first = asyncio.run(evaluate_opportunity(rule, context, spot_price=102, hist_df=history))
    second = asyncio.run(evaluate_opportunity(rule, context, spot_price=102, hist_df=history))
    assert first.total_score == second.total_score
    assert first.dividend_bond_spread == pytest.approx(3.4)
    assert 0 <= first.total_score <= 100


def test_realtime_price_does_not_complete_251_day_history(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0] * 251},
        index=pd.date_range("2025-01-01", periods=251),
    )
    context = SimpleNamespace(bot_data={})
    monkeypatch.setattr(
        "src.opportunity.get_history_data_cached",
        AsyncMock(return_value=history),
    )
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation",
        AsyncMock(return_value=None),
    )

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), context, spot_price=90, hist_df=history)
    )

    assert snapshot.high_52w is None
    assert snapshot.drawdown_52w is None


def test_unadjusted_etf_fallback_disables_long_term_metrics(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    history.attrs["price_basis"] = "unadjusted_fallback"
    context = SimpleNamespace(bot_data={})
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation",
        AsyncMock(return_value=None),
    )

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), context, spot_price=90, hist_df=history)
    )

    assert snapshot.ma200 is None
    assert snapshot.high_52w is None
    assert snapshot.long_term_score == 0
    assert "Adjusted ETF history unavailable" in snapshot.data_notes[0]


def test_missing_realtime_adjustment_factor_marks_qfq_fallback_degraded(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    history.attrs.update(price_basis="qfq", adjust_factor=None)
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("src.opportunity._quality", lambda *args: "OK")

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), spot_price=90, hist_df=history)
    )

    assert snapshot.technical_price_basis == "qfq_history_close"
    assert snapshot.data_quality == "DEGRADED"
    assert any("adjustment factor unavailable" in note.lower() for note in snapshot.data_notes)


def test_percentile_maturity_requires_observations_and_real_span():
    short_span = [(date(2024, 1, 1) + timedelta(days=round(i * 1.2 * 365 / 299)), 4.0) for i in range(300)]
    mature_span = [(date(2024, 1, 1) + timedelta(days=round(i * 2.2 * 365 / 299)), 4.0) for i in range(300)]

    assert _history_maturity(short_span) == pytest.approx((300, 1.2, False), abs=0.01)
    assert _history_maturity(mature_span) == pytest.approx((300, 2.2, True), abs=0.01)


def test_spread_maturity_uses_dates_of_matched_observations():
    valuation_rows = [
        {"valuation_date": (date(2024, 1, 1) + timedelta(days=round(i * 1.2 * 365 / 299))).isoformat(), "dividend_yield2": 4.0}
        for i in range(300)
    ]
    bond_rows = [
        {"yield_date": row["valuation_date"], "cn10y": 1.5}
        for row in valuation_rows
    ]

    spreads = _history_spreads(valuation_rows, bond_rows, "股息率2")

    assert len(spreads) == 300
    assert _history_maturity(spreads)[2] is False
