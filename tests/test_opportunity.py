import asyncio
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

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
        technical_price_basis="qfq_history_close",
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
    assert "使用 /opcheck 1" in alert
    assert "技术价格：2026-08-24" in detail
    assert "前复权实时价（qfq）" in detail and "中证估值：2026-08-23" in detail


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


def test_evaluate_opportunity_is_deterministic_with_supplied_data(
    monkeypatch, mock_calendar_preload
):
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
    assert mock_calendar_preload.await_count == 2
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


def test_implicit_quote_fetch_uses_shared_failure_tracking(monkeypatch):
    from src.data_fetcher import RealtimeQuote

    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    quote = RealtimeQuote(101.0)
    fetch = AsyncMock(return_value=({"510300": quote}, True))
    monkeypatch.setattr("src.opportunity._fetch_all_realtime_quotes", fetch)
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation",
        AsyncMock(return_value=None),
    )

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), hist_df=history)
    )

    fetch.assert_awaited_once()
    assert fetch.await_args.args[1] == ["510300"]
    assert snapshot.spot_price == 101.0


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
    history_fetch = AsyncMock(side_effect=AssertionError("supplied history must stay deterministic"))
    monkeypatch.setattr("src.opportunity.get_history_data_cached", history_fetch)

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), context, spot_price=90, hist_df=history)
    )

    assert snapshot.ma200 is None
    assert snapshot.high_52w is None
    assert snapshot.rsi6 is None
    assert snapshot.long_term_score == 0
    assert "ETF 复权历史数据不可用" in snapshot.data_notes[0]
    history_fetch.assert_not_awaited()


def test_degraded_detail_separates_spot_and_technical_price():
    snapshot = _snapshot(48, "WATCH")
    snapshot.price = 1.433
    snapshot.spot_price = 1.453
    snapshot.technical_price_date = "2026-08-21"
    snapshot.technical_price_basis = "unavailable"

    detail = format_opportunity_detail(snapshot)

    assert "⚠️ <b>部分评分</b>" in detail
    assert "现价：1.453" in detail
    assert "技术价：1.433" in detail
    assert "技术日期：2026-08-21" in detail
    assert "当前价：1.433" not in detail


def test_alert_gate_suppresses_only_missing_runtime_technical_basis():
    rule = _rule(last_score=40, min_score=60)
    degraded = _snapshot(70, "MODERATE")
    degraded.technical_price_basis = "unavailable"
    assert should_send_opportunity_alert(rule, degraded)[0] is False

    qfq_with_thin_valuation_history = _snapshot(70, "MODERATE")
    qfq_with_thin_valuation_history.technical_price_basis = "qfq_history_close"
    qfq_with_thin_valuation_history.data_quality = "INSUFFICIENT_VALUATION_HISTORY"
    assert should_send_opportunity_alert(rule, qfq_with_thin_valuation_history)[0] is True


def test_recovered_qfq_history_restores_all_technical_factors(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0 + i * 0.01 for i in range(550)]},
        index=pd.date_range("2025-01-01", periods=550),
    )
    history.attrs.update(
        technical_history_days=550,
        price_basis="qfq",
        price_basis_asof="2026-08-24",
    )
    monkeypatch.setattr("src.opportunity.get_cached_valuation", AsyncMock(return_value=None))
    history_fetch = AsyncMock(side_effect=AssertionError("supplied qfq history must stay deterministic"))
    monkeypatch.setattr("src.opportunity.get_history_data_cached", history_fetch)

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), spot_price=105.5, hist_df=history)
    )

    assert snapshot.technical_price_basis == "qfq_history_close"
    assert snapshot.ma200 is not None
    assert snapshot.high_52w is not None
    assert snapshot.rsi6 is not None
    history_fetch.assert_not_awaited()


def test_production_refresh_cannot_score_stale_qfq_history(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0 + i * 0.01 for i in range(550)]},
        index=pd.date_range("2025-01-01", periods=550),
    )
    history.attrs.update(
        technical_history_days=550,
        price_basis="qfq",
        price_basis_asof="2026-08-23",
    )
    monkeypatch.setattr(
        "src.opportunity._now",
        lambda: datetime(2026, 8, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr(
        "src.opportunity.get_history_data_cached", AsyncMock(return_value=history)
    )
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation", AsyncMock(return_value=None)
    )

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), spot_price=105.5)
    )

    assert snapshot.technical_price_basis == "unavailable"
    assert snapshot.ma200 is None
    assert snapshot.high_52w is None
    assert snapshot.rsi6 is None


def test_future_valuation_date_is_stale_and_cannot_raise_level(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    history.attrs.update(price_basis="qfq", price_basis_asof="2026-08-24")
    monkeypatch.setattr("src.data_fetcher.is_trading_day", lambda now: True)
    monkeypatch.setattr("src.opportunity._now", lambda: datetime(2026, 8, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation",
        AsyncMock(return_value={
            "valuation_date": "2026-08-25",
            "dividend_yield1": 8.0,
            "dividend_yield2": 8.0,
            "pe1": 8.0,
            "pe2": 8.0,
        }),
    )
    monkeypatch.setattr("src.opportunity.get_cached_cn10y", AsyncMock(return_value=None))
    monkeypatch.setattr("src.opportunity.get_valuation_history", lambda *args: [])
    monkeypatch.setattr("src.opportunity.get_bond_history", lambda *args, **kwargs: [])

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), spot_price=90, hist_df=history)
    )

    assert snapshot.level not in {"MODERATE", "STRONG", "RARE"}
    assert snapshot.data_quality == "STALE_VALUATION"
    assert "估值日期在未来，新鲜度无效" in snapshot.data_notes


def test_unavailable_calendar_uses_valuation_safety_gate(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    history.attrs.update(price_basis="qfq", price_basis_asof="2026-08-24")
    valuation = {
        "valuation_date": "2026-08-23",
        "dividend_yield1": 8.0,
        "dividend_yield2": 8.0,
        "pe1": 8.0,
        "pe2": 8.0,
    }
    bond = {"yield_date": "2026-08-23", "cn10y": 1.5, "source": "chinabond"}
    monkeypatch.setattr(
        "src.opportunity._now",
        lambda: datetime(2026, 8, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr("src.opportunity.trading_sessions_elapsed", lambda *args: None)
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation", AsyncMock(return_value=valuation)
    )
    monkeypatch.setattr("src.opportunity.get_cached_cn10y", AsyncMock(return_value=bond))
    monkeypatch.setattr("src.opportunity.get_valuation_history", lambda *args: [])
    monkeypatch.setattr(
        "src.opportunity.get_bond_history", lambda *args, **kwargs: [bond]
    )

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), spot_price=90, hist_df=history)
    )

    assert snapshot.level == "WATCH"
    assert snapshot.data_quality == "STALE_VALUATION"
    assert "交易日历新鲜度不可用，已启用估值安全门控" in snapshot.data_notes


def test_unconfirmed_qfq_basis_marks_qfq_fallback_degraded(monkeypatch):
    history = pd.DataFrame(
        {"收盘": [100.0] * 300},
        index=pd.date_range("2025-01-01", periods=300),
    )
    history.attrs.update(price_basis="qfq", price_basis_asof="2026-08-21")
    monkeypatch.setattr(
        "src.opportunity.get_cached_valuation", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("src.opportunity._quality", lambda *args: "OK")

    snapshot = asyncio.run(
        evaluate_opportunity(_rule(), SimpleNamespace(bot_data={}), spot_price=90, hist_df=history)
    )

    assert snapshot.technical_price_basis == "qfq_history_close"
    assert snapshot.data_quality == "DEGRADED"
    assert any("基准尚未确认" in note for note in snapshot.data_notes)


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
