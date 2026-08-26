"""Opportunity evaluation orchestration and alert/snapshot helpers."""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .config import (
    CSI_DIVIDEND_YIELD_FIELD,
    DATA_QUALITY_LABELS,
    MIN_VALUATION_SCORE_FOR_OPPORTUNITY,
    OPPORTUNITY_ALERT_COOLDOWN_MINUTES,
    OPPORTUNITY_LEVEL_LABELS,
    OPPORTUNITY_MAX_ALERTS_PER_DAY,
    RSI_PERIOD,
    SCORING_MODE_LABELS,
    TECHNICAL_BASIS_LABELS,
    TECHNICAL_HISTORY_DAYS,
    VALUATION_PERCENTILE_LOOKBACK_YEARS,
    VALUATION_PERCENTILE_MIN_OBS,
    VALUATION_PERCENTILE_MIN_SPAN_YEARS,
    VALUATION_STALE_MAX_TRADING_DAYS,
)
from .data_fetcher import (
    RealtimeQuote,
    _fetch_single_realtime_quote,
    build_indicator_close_series,
    calculate_rsi,
    get_history_data_cached,
    runtime_history_is_usable,
)
from .database import db_execute
from .market import trading_sessions_elapsed
from .metrics import (
    calculate_52w_drawdown,
    calculate_52w_high,
    calculate_ma200,
    calculate_ma200_deviation,
    calculate_percentile,
    classify_opportunity_level,
    is_level_upgrade,
    score_dividend_bond_spread,
    score_dividend_yield,
    score_drawdown,
    score_ma200,
    score_rsi,
    total_score,
    valid_close_count,
)
from .utils import split_message
from .valuation_fetcher import (
    get_bond_history,
    get_cached_cn10y,
    get_cached_valuation,
    get_valuation_history,
)

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class OpportunitySnapshot:
    rule_id: int
    asset_code: str
    asset_name: str
    benchmark_code: str
    benchmark_name: str
    snapshot_at: str
    price: Optional[float] = None
    spot_price: Optional[float] = None
    rsi6: Optional[float] = None
    ma200: Optional[float] = None
    ma200_deviation: Optional[float] = None
    high_52w: Optional[float] = None
    drawdown_52w: Optional[float] = None
    pe1: Optional[float] = None
    pe2: Optional[float] = None
    dividend_yield1: Optional[float] = None
    dividend_yield2: Optional[float] = None
    dividend_yield_used: Optional[float] = None
    dividend_yield_percentile: Optional[float] = None
    cn10y: Optional[float] = None
    dividend_bond_spread: Optional[float] = None
    spread_percentile: Optional[float] = None
    dividend_yield_score: float = 0
    spread_score: float = 0
    valuation_score: float = 0
    long_term_score: float = 0
    tactical_score: float = 0
    total_score: float = 0
    level: str = "NEUTRAL"
    scoring_mode: str = "NONE"
    data_quality: str = "DEGRADED"
    data_notes: list[str] = field(default_factory=list)
    valuation_date: Optional[str] = None
    cn10y_date: Optional[str] = None
    cn10y_source: Optional[str] = None
    technical_price_date: Optional[str] = None
    technical_price_basis: str = "unavailable"


def _now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def _date_or_none(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _row_value(row, key: str, default=None):
    """Read dict-like provider/database rows without assuming every column exists."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _display_label(labels: dict[str, str], value: str | None) -> str:
    return labels.get(value, value) if value else '暂无'


def _match_bond(rows, target_date: date):
    candidates = []
    for row in rows:
        row_date = _date_or_none(row["yield_date"])
        if row_date is not None and row_date <= target_date:
            candidates.append((row_date, row))
    if not candidates:
        return None
    row_date, matched = max(candidates, key=lambda item: item[0])
    return matched if row_date and (target_date - row_date).days <= 7 else None


def _history_spreads(valuation_rows, bond_rows, field: str) -> list[tuple[date, float]]:
    field = "dividend_yield1" if field == "股息率1" else "dividend_yield2"
    spreads = []
    for row in valuation_rows:
        value = _float_or_none(row[field])
        valuation_date = _date_or_none(row["valuation_date"])
        if value is None or valuation_date is None:
            continue
        bond = _match_bond(bond_rows, valuation_date)
        if bond is not None:
            cn10y = _float_or_none(bond["cn10y"])
            if cn10y is not None:
                spreads.append((valuation_date, value - cn10y))
    return spreads


def _dated_values(rows, value_field: str) -> list[tuple[date, float]]:
    """Keep only valid valuation observations together with their dates."""
    values = []
    for row in rows:
        value = _float_or_none(row[value_field])
        row_date = _date_or_none(row["valuation_date"])
        if value is not None and row_date is not None:
            values.append((row_date, value))
    return values


def _history_maturity(
    dated_values: list[tuple[date, float]],
    min_observations: int = VALUATION_PERCENTILE_MIN_OBS,
    min_span_years: float = VALUATION_PERCENTILE_MIN_SPAN_YEARS,
) -> tuple[int, float, bool]:
    """Return count, actual date span, and mature-percentile eligibility."""
    valid = [
        (row_date, value)
        for row_date, value in dated_values
        if isinstance(row_date, date) and _float_or_none(value) is not None
    ]
    if not valid:
        return 0, 0.0, False
    dates = [row_date for row_date, _ in valid]
    span_years = (max(dates) - min(dates)).days / 365.0 if len(dates) >= 2 else 0.0
    count = len(valid)
    return count, span_years, count >= min_observations and span_years >= min_span_years


def _quality(
    valuation_available: bool,
    stale: bool,
    bond_available: bool,
    technical_complete: bool,
    dy_history_complete: bool,
    spread_history_complete: bool,
) -> str:
    if not valuation_available:
        return "VALUATION_UNAVAILABLE"
    if stale:
        return "STALE_VALUATION"
    if not bond_available:
        return "BOND_YIELD_UNAVAILABLE"
    if not technical_complete and (not dy_history_complete or not spread_history_complete):
        return "DEGRADED"
    if not technical_complete:
        return "INSUFFICIENT_TECHNICAL_HISTORY"
    if not dy_history_complete or not spread_history_complete:
        return "INSUFFICIENT_VALUATION_HISTORY"
    return "OK"


async def evaluate_opportunity(
    rule,
    context,
    spot_price: Optional[float] = None,
    hist_df=None,
    quote: Optional[RealtimeQuote] = None,
) -> OpportunitySnapshot:
    """Evaluate one rule; network data is supplied by shared caches where possible."""
    now = _now()
    bot_data = context.bot_data
    asset_code = str(rule["asset_code"])
    benchmark_code = str(rule["benchmark_code"])
    asset_name = str(rule["asset_name"] or asset_code)
    benchmark_name = str(rule["benchmark_name"] or benchmark_code)
    notes: list[str] = []

    cache = bot_data.setdefault("hist_data_cache", {})
    supplied_history = hist_df is not None
    if hist_df is None:
        hist_df = cache.get(asset_code)
    if not supplied_history and not runtime_history_is_usable(
        hist_df, TECHNICAL_HISTORY_DAYS, now
    ):
        fetched = await get_history_data_cached(context, asset_code, TECHNICAL_HISTORY_DAYS)
        if fetched is not None and not fetched.empty:
            cache[asset_code] = fetched
            hist_df = fetched
    if quote is None and isinstance(spot_price, RealtimeQuote):
        quote = spot_price
        spot_price = quote.price
    if quote is None and spot_price is None:
        quote = await _fetch_single_realtime_quote(asset_code)
    elif quote is None and spot_price is not None:
        quote = RealtimeQuote(float(spot_price), None)
    if spot_price is None and quote is not None:
        spot_price = quote.price

    indicator_series = build_indicator_close_series(hist_df, quote, now=now)
    closes = indicator_series.closes
    current_price = _float_or_none(indicator_series.current_price)
    historical_closes = (
        valid_close_count(hist_df["收盘"])
        if hist_df is not None and not hist_df.empty and "收盘" in hist_df.columns
        else 0
    )
    technical_basis_available = (
        hist_df is not None
        and not hist_df.empty
        and hist_df.attrs.get("price_basis") == "qfq"
        and not closes.empty
        and (
            supplied_history
            or runtime_history_is_usable(hist_df, TECHNICAL_HISTORY_DAYS, now)
        )
    )
    ma200 = calculate_ma200(closes) if technical_basis_available and historical_closes >= 200 else None
    ma_deviation = calculate_ma200_deviation(current_price, ma200)
    high_52w = calculate_52w_high(closes) if technical_basis_available and historical_closes >= 252 else None
    drawdown = calculate_52w_drawdown(current_price, high_52w)
    rsi6 = calculate_rsi(closes) if technical_basis_available and not closes.empty else None
    technical_complete = ma200 is not None and high_52w is not None
    technical_price_basis = (
        "qfq_realtime" if technical_basis_available and indicator_series.spot_used
        else "qfq_history_close" if technical_basis_available
        else "unavailable"
    )
    technical_price_date = (
        indicator_series.price_date.isoformat()
        if indicator_series.price_date is not None else None
    )
    if indicator_series.note:
        notes.append(indicator_series.note)
    if not technical_basis_available:
        notes.append("复权历史数据不可用，已停用 MA200、52 周回撤和 RSI")
    elif ma200 is None:
        notes.append("MA200 不可用：有效收盘价少于 200 条")
    if technical_basis_available and high_52w is None:
        notes.append("52 周回撤不可用：有效收盘价少于 252 条")
    if hist_df is None or hist_df.empty:
        notes.append("技术历史数据不可用")

    valuation = await get_cached_valuation(benchmark_code, bot_data)
    valuation_date = _date_or_none(_row_value(valuation, "valuation_date")) if valuation else None
    valuation_field = "dividend_yield1" if CSI_DIVIDEND_YIELD_FIELD == "股息率1" else "dividend_yield2"
    valuation_available = valuation is not None and _float_or_none(valuation[valuation_field]) is not None
    dividend_yield1 = _float_or_none(valuation["dividend_yield1"]) if valuation else None
    dividend_yield2 = _float_or_none(valuation["dividend_yield2"]) if valuation else None
    dividend_yield_used = _float_or_none(valuation[valuation_field]) if valuation else None
    pe1 = _float_or_none(valuation["pe1"]) if valuation else None
    pe2 = _float_or_none(valuation["pe2"]) if valuation else None
    stale = False
    if not valuation:
        notes.append("股息率不可用，已启用估值安全门控")
    elif not valuation_available:
        notes.append(f"所选股息率字段不可用：{CSI_DIVIDEND_YIELD_FIELD}")
    elif valuation_date is None:
        # A value without a date cannot be trusted as fresh.  Keep the value
        # visible for diagnostics, but let the normal stale gate cap the level.
        stale = True
        notes.append("估值日期不可用，无法判断数据新鲜度")
    elif valuation_date > now.date():
        stale = True
        notes.append("估值日期在未来，新鲜度无效")
    else:
        try:
            sessions = trading_sessions_elapsed(valuation_date, now.date())
        except Exception as exc:
            logger.warning("交易日历新鲜度判断失败: %s", exc)
            sessions = None
        if sessions is None:
            stale = (now.date() - valuation_date).days > 14
            notes.append("交易日历新鲜度不可用，已回退到自然日判断")
            if stale:
                notes.append(
                    f"估值日期 {valuation_date.isoformat()} 已超过 14 个自然日"
                )
        else:
            stale = sessions > VALUATION_STALE_MAX_TRADING_DAYS
            if stale:
                notes.append(
                    f"估值日期 {valuation_date.isoformat()} 已过去 {sessions} 个交易日 "
                    f"（上限 {VALUATION_STALE_MAX_TRADING_DAYS}）"
                )

    dy_percentile = None
    spread_percentile = None
    spread = None
    cn10y = None
    cn10y_date = None
    cn10y_source = None
    dy_history_complete = False
    spread_history_complete = False
    if valuation and valuation_date:
        cutoff = (
            pd.Timestamp(valuation_date)
            - pd.DateOffset(years=VALUATION_PERCENTILE_LOOKBACK_YEARS)
        ).date()
        valuation_rows = get_valuation_history(benchmark_code, cutoff, valuation_date)
        dy_dated_values = _dated_values(valuation_rows, valuation_field)
        dy_history = [value for _, value in dy_dated_values]
        dy_observations, dy_span_years, dy_history_complete = _history_maturity(dy_dated_values)
        if dy_history_complete and dividend_yield_used is not None:
            dy_percentile = calculate_percentile(dividend_yield_used, dy_history)
            notes.append(
                f"股息率历史：{dy_observations} 条，跨度 {dy_span_years:.1f} 年；已使用历史分位"
            )
        else:
            notes.append(
                f"股息率历史分位不可用：{dy_observations} 条，跨度 {dy_span_years:.1f} 年；已使用绝对值阈值"
            )

        latest_bond = await get_cached_cn10y(bot_data)
        bond_rows = get_bond_history(end_date=valuation_date)
        matched_bond = _match_bond(bond_rows, valuation_date)
        if matched_bond is None and latest_bond is not None:
            matched_bond = _match_bond([latest_bond], valuation_date)
        if matched_bond is not None:
            cn10y = _float_or_none(matched_bond["cn10y"])
            cn10y_date = matched_bond["yield_date"]
            cn10y_source = matched_bond["source"]
            if cn10y is not None and dividend_yield_used is not None:
                spread = dividend_yield_used - cn10y
        else:
            notes.append("估值日期前 7 个自然日内没有中国十年期国债数据")

        spread_rows = _history_spreads(valuation_rows, bond_rows, CSI_DIVIDEND_YIELD_FIELD)
        spread_values = [value for _, value in spread_rows]
        spread_observations, spread_span_years, spread_history_complete = _history_maturity(spread_rows)
        if spread_history_complete and spread is not None:
            spread_percentile = calculate_percentile(spread, spread_values)
            notes.append(
                f"股息率—国债利差历史：{spread_observations} 条，跨度 {spread_span_years:.1f} 年；已使用历史分位"
            )
        elif not spread_history_complete:
            notes.append(
                f"股息率—国债利差分位不可用：{spread_observations} 条，跨度 {spread_span_years:.1f} 年；已使用绝对值阈值"
            )
    else:
        notes.append("依赖估值数据的利差不可用")

    dividend_yield_score = score_dividend_yield(dividend_yield_used, dy_percentile)
    spread_score = score_dividend_bond_spread(spread, spread_percentile)
    valuation_score = total_score(dividend_yield_score, spread_score)
    long_term_score = total_score(score_ma200(ma_deviation), score_drawdown(drawdown))
    tactical_score = score_rsi(rsi6)
    score = total_score(valuation_score, long_term_score, tactical_score)
    if not valuation_available:
        scoring_mode = "NONE"
    elif dy_percentile is not None and spread_percentile is not None:
        scoring_mode = "PERCENTILE"
    elif dy_percentile is None and spread_percentile is None:
        scoring_mode = "ABSOLUTE_FALLBACK"
    else:
        scoring_mode = "MIXED"
    level = classify_opportunity_level(
        score,
        valuation_available=valuation_available,
        valuation_score=valuation_score,
        stale_valuation=stale,
        min_valuation_score=MIN_VALUATION_SCORE_FOR_OPPORTUNITY,
        scoring_mode=scoring_mode,
    )
    data_quality = _quality(
        valuation_available,
        stale,
        cn10y is not None,
        technical_complete,
        dy_history_complete,
        spread_history_complete,
    )
    if not technical_basis_available and data_quality == "INSUFFICIENT_TECHNICAL_HISTORY":
        data_quality = "DEGRADED"
    if data_quality == "OK" and indicator_series.degraded:
        data_quality = "DEGRADED"
    if cn10y_source == "sina":
        notes.append("中国十年期国债数据来源：Sina 回退源")

    snapshot = OpportunitySnapshot(
        rule_id=int(rule["id"]),
        asset_code=asset_code,
        asset_name=asset_name,
        benchmark_code=benchmark_code,
        benchmark_name=benchmark_name,
        snapshot_at=now.isoformat(),
        price=current_price,
        spot_price=_float_or_none(spot_price),
        rsi6=rsi6,
        ma200=ma200,
        ma200_deviation=ma_deviation,
        high_52w=high_52w,
        drawdown_52w=drawdown,
        pe1=pe1,
        pe2=pe2,
        dividend_yield1=dividend_yield1,
        dividend_yield2=dividend_yield2,
        dividend_yield_used=dividend_yield_used,
        dividend_yield_percentile=dy_percentile,
        cn10y=cn10y,
        dividend_bond_spread=spread,
        spread_percentile=spread_percentile,
        dividend_yield_score=dividend_yield_score,
        spread_score=spread_score,
        valuation_score=valuation_score,
        long_term_score=long_term_score,
        tactical_score=tactical_score,
        total_score=score,
        level=level,
        scoring_mode=scoring_mode,
        data_quality=data_quality,
        data_notes=notes,
        valuation_date=_row_value(valuation, "valuation_date") if valuation else None,
        cn10y_date=cn10y_date,
        cn10y_source=cn10y_source,
        technical_price_date=technical_price_date,
        technical_price_basis=technical_price_basis,
    )
    logger.info(
        "[OPPORTUNITY] %s / asset %s DY=%s CN10Y=%s Spread=%s MADev=%s DD52=%s RSI6=%s Score=%s Level=%s Mode=%s",
        benchmark_code,
        asset_code,
        dividend_yield_used,
        cn10y,
        spread,
        ma_deviation,
        drawdown,
        rsi6,
        score,
        level,
        scoring_mode,
    )
    return snapshot


def latest_opportunity_snapshot(rule_id: int):
    return db_execute(
        "SELECT * FROM opportunity_snapshots WHERE rule_id = ? ORDER BY snapshot_at DESC LIMIT 1",
        (rule_id,),
        fetchone=True,
    )


def snapshot_should_persist(rule_id: int, snapshot: OpportunitySnapshot, alert_sent: bool = False) -> bool:
    if alert_sent:
        return True
    previous = latest_opportunity_snapshot(rule_id)
    if previous is None:
        return True
    if previous["level"] != snapshot.level:
        return True
    return abs(float(previous["total_score"] or 0) - snapshot.total_score) >= 5


def save_opportunity_snapshot(
    snapshot: OpportunitySnapshot,
    alert_sent: bool = False,
    critical: bool = False,
) -> None:
    """Save a snapshot; alert/explicit critical writes propagate DB errors."""
    db_execute(
        """
        INSERT INTO opportunity_snapshots (
            rule_id, snapshot_at, price, rsi6, ma200, ma200_deviation,
            high_52w, drawdown_52w, pe1, pe2, dividend_yield1, dividend_yield2,
            dividend_yield_used, dividend_yield_percentile, cn10y,
            dividend_bond_spread, spread_percentile, dividend_yield_score,
            spread_score, valuation_score, long_term_score, tactical_score,
            total_score, level, scoring_mode, data_quality, data_notes,
            valuation_date, cn10y_date, cn10y_source,
            technical_price_date, technical_price_basis, alert_sent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.rule_id,
            snapshot.snapshot_at,
            snapshot.price,
            snapshot.rsi6,
            snapshot.ma200,
            snapshot.ma200_deviation,
            snapshot.high_52w,
            snapshot.drawdown_52w,
            snapshot.pe1,
            snapshot.pe2,
            snapshot.dividend_yield1,
            snapshot.dividend_yield2,
            snapshot.dividend_yield_used,
            snapshot.dividend_yield_percentile,
            snapshot.cn10y,
            snapshot.dividend_bond_spread,
            snapshot.spread_percentile,
            snapshot.dividend_yield_score,
            snapshot.spread_score,
            snapshot.valuation_score,
            snapshot.long_term_score,
            snapshot.tactical_score,
            snapshot.total_score,
            snapshot.level,
            snapshot.scoring_mode,
            snapshot.data_quality,
            json.dumps(snapshot.data_notes, ensure_ascii=False),
            snapshot.valuation_date,
            snapshot.cn10y_date,
            snapshot.cn10y_source,
            snapshot.technical_price_date,
            snapshot.technical_price_basis,
            int(alert_sent),
        ),
        swallow_errors=not (critical or alert_sent),
    )


def record_rule_evaluation(rule_id: int, snapshot: OpportunitySnapshot, now: Optional[datetime] = None) -> None:
    db_execute(
        "UPDATE opportunity_rules SET last_score = ?, last_level = ?, updated_at = ? WHERE id = ?",
        (snapshot.total_score, snapshot.level, (now or _now()).isoformat(), rule_id),
        swallow_errors=False,
    )


def record_rule_alert(rule_id: int, snapshot: OpportunitySnapshot, now: Optional[datetime] = None) -> None:
    alert_at = (now or _now()).isoformat()
    db_execute(
        """
        UPDATE opportunity_rules
        SET last_alert_score = ?, last_alert_level = ?, last_alert_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (snapshot.total_score, snapshot.level, alert_at, alert_at, rule_id),
        swallow_errors=False,
    )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def should_send_opportunity_alert(
    rule,
    snapshot: OpportunitySnapshot,
    now: Optional[datetime] = None,
    alerts_today: int = 0,
) -> tuple[bool, str]:
    now = now or _now()
    if snapshot.technical_price_basis == "unavailable":
        return False, "technical-data-unavailable"
    threshold = float(rule["min_score"])
    previous_score = _float_or_none(rule["last_score"])
    previous_level = rule["last_level"]
    if previous_score is None or snapshot.total_score < threshold:
        return False, "below-threshold-or-no-baseline"
    crossed_threshold = previous_score < threshold <= snapshot.total_score
    upgraded = is_level_upgrade(previous_level, snapshot.level) and snapshot.total_score >= threshold
    if not crossed_threshold and not upgraded:
        return False, "no-crossing"

    override = upgraded
    if not override and alerts_today >= OPPORTUNITY_MAX_ALERTS_PER_DAY:
        return False, "daily-limit"
    last_alert_at = _parse_datetime(rule["last_alert_at"])
    if not override and last_alert_at is not None:
        elapsed = (now - last_alert_at).total_seconds() / 60
        if elapsed < OPPORTUNITY_ALERT_COOLDOWN_MINUTES:
            return False, "cooldown"
    return True, "level-upgrade" if upgraded else "threshold-crossing"


def format_opportunity_detail(snapshot: OpportunitySnapshot, alert_reason: Optional[str] = None) -> str:
    icon = {"NEUTRAL": "⚪", "WATCH": "🟡", "MODERATE": "🟢", "STRONG": "🟢", "RARE": "🔥"}.get(snapshot.level, "⚪")
    safe_asset = html.escape(snapshot.asset_name)
    safe_benchmark = html.escape(snapshot.benchmark_name)

    def f(value, digits=2, suffix=""):
        return "暂无" if value is None else f"{float(value):.{digits}f}{suffix}"

    percentile = "暂无" if snapshot.dividend_yield_percentile is None else f"{snapshot.dividend_yield_percentile * 100:.0f}%"
    spread_percentile = "暂无" if snapshot.spread_percentile is None else f"{snapshot.spread_percentile * 100:.0f}%"
    notes = "\n".join(f"- {html.escape(note)}" for note in snapshot.data_notes) or "- 无"
    trigger = {
        "threshold-crossing": "分数跨过该规则告警阈值",
        "level-upgrade": "机会等级升级",
    }.get(alert_reason or "")
    trigger_line = f"触发原因：<b>{trigger}</b>\n\n" if trigger else ""
    partial_warning = (
        "⚠️ <b>部分评分</b>\n\n"
        "复权技术历史数据不可用。\n"
        "本次结果不包含 MA200 / 52 周回撤 / RSI6。\n"
        "请勿将其理解为完整的 0–100 红利机会评分。\n\n"
        if snapshot.technical_price_basis == "unavailable"
        else ""
    )
    price_lines = (
        f"当前价：{f(snapshot.spot_price if snapshot.spot_price is not None else snapshot.price, 3)}\n"
        if snapshot.technical_price_basis == "qfq_realtime"
        else (
            f"现价：{f(snapshot.spot_price, 3)}\n"
            f"技术价：{f(snapshot.price, 3)}\n"
            f"技术日期：{html.escape(snapshot.technical_price_date or '暂无')}\n"
            f"价格基准：{html.escape(_display_label(TECHNICAL_BASIS_LABELS, snapshot.technical_price_basis))}\n"
        )
    )
    return (
        f"{icon} <b>红利机会 · {_display_label(OPPORTUNITY_LEVEL_LABELS, snapshot.level)}</b>\n"
        f"评分：<b>{snapshot.total_score:.0f} / 100</b>\n\n"
        f"{partial_warning}"
        f"{trigger_line}"
        f"{safe_asset} (<code>{html.escape(snapshot.asset_code)}</code>)\n"
        f"估值基准：{safe_benchmark} (<code>{html.escape(snapshot.benchmark_code)}</code>)\n\n"
        f"📊 <b>估值</b> {snapshot.valuation_score:.0f} / 50\n\n"
        f"股息率\n"
        f"D/P1：{f(snapshot.dividend_yield1, 2, '%')}\n"
        f"D/P2：{f(snapshot.dividend_yield2, 2, '%')}（使用：{CSI_DIVIDEND_YIELD_FIELD}）\n\n"
        f"PE1：{f(snapshot.pe1, 2)}\n"
        f"PE2：{f(snapshot.pe2, 2)}\n\n"
        f"历史分位：{percentile}\n"
        f"利差分位：{spread_percentile}\n"
        f"评分模式：<code>{_display_label(SCORING_MODE_LABELS, snapshot.scoring_mode)}</code>\n\n"
        f"中国十年期国债：{f(snapshot.cn10y, 2, '%')}"
        f"（{html.escape(snapshot.cn10y_source or '暂无')}）\n"
        f"股息率—国债利差：{f(snapshot.dividend_bond_spread, 2, ' 个百分点')}\n\n"
        f"📉 <b>长期</b> {snapshot.long_term_score:.0f} / 30\n\n"
        f"{price_lines}"
        f"MA200：{f(snapshot.ma200, 3)}\n"
        f"偏离度：{f(None if snapshot.ma200_deviation is None else snapshot.ma200_deviation * 100, 2, '%')}\n"
        f"52 周高点：{f(snapshot.high_52w, 3)}\n"
        f"回撤：{f(None if snapshot.drawdown_52w is None else snapshot.drawdown_52w * 100, 2, '%')}\n\n"
        f"⚡ <b>战术</b> {snapshot.tactical_score:.0f} / 20\n\n"
        f"RSI({RSI_PERIOD})：{f(snapshot.rsi6, 2)}\n\n"
        f"🧮 <b>评分明细</b>\n"
        f"股息率：{snapshot.dividend_yield_score:.0f} / 30\n"
        f"股息率—国债利差：{snapshot.spread_score:.0f} / 20\n"
        f"MA200：{score_ma200(snapshot.ma200_deviation):.0f} / 20\n"
        f"52 周回撤：{score_drawdown(snapshot.drawdown_52w):.0f} / 10\n"
        f"RSI6：{score_rsi(snapshot.rsi6):.0f} / 20\n\n"
        f"总分：<b>{snapshot.total_score:.0f} / 100</b>\n\n"
        f"📅 <b>数据日期</b>\n"
        f"技术价格：{html.escape(snapshot.technical_price_date or '暂无')}\n"
        f"价格基准：<code>{html.escape(_display_label(TECHNICAL_BASIS_LABELS, snapshot.technical_price_basis))}</code>\n"
        f"中证估值：{html.escape(snapshot.valuation_date or '暂无')}\n"
        f"中国十年期国债：{html.escape(snapshot.cn10y_date or '暂无')}\n"
        f"数据来源：{html.escape(snapshot.cn10y_source or '暂无')}\n\n"
        f"数据质量：<code>{_display_label(DATA_QUALITY_LABELS, snapshot.data_quality)}</code>\n"
        f"备注：\n{notes}"
    )


def format_opportunity_alert(snapshot: OpportunitySnapshot, reason: Optional[str] = None) -> str:
    """Compact automatic alert; full audit remains available via /opcheck."""
    icon = {"NEUTRAL": "⚪", "WATCH": "🟡", "MODERATE": "🟢", "STRONG": "🟢", "RARE": "🔥"}.get(snapshot.level, "⚪")

    def f(value, digits=2, suffix=""):
        return "暂无" if value is None else f"{float(value):.{digits}f}{suffix}"

    trigger = {
        "threshold-crossing": "分数跨过告警阈值",
        "level-upgrade": "机会等级升级",
    }.get(reason or "", "机会告警")
    return (
        f"{icon} <b>红利机会 · {_display_label(OPPORTUNITY_LEVEL_LABELS, snapshot.level)}</b> — <b>{snapshot.total_score:.0f}/100</b>\n\n"
        f"{html.escape(snapshot.asset_name)} (<code>{html.escape(snapshot.asset_code)}</code>)\n"
        f"估值基准：{html.escape(snapshot.benchmark_name)}\n\n"
        f"估值           {snapshot.valuation_score:.0f}/50\n"
        f"长期           {snapshot.long_term_score:.0f}/30\n"
        f"战术           {snapshot.tactical_score:.0f}/20\n\n"
        f"股息率         {f(snapshot.dividend_yield_used, 2, '%')}\n"
        f"股息率—国债   {f(snapshot.dividend_bond_spread, 2, '个百分点')}\n"
        f"MA200          {f(None if snapshot.ma200_deviation is None else snapshot.ma200_deviation * 100, 1, '%')}\n"
        f"52 周回撤      {f(None if snapshot.drawdown_52w is None else snapshot.drawdown_52w * 100, 1, '%')}\n"
        f"RSI{RSI_PERIOD}            {f(snapshot.rsi6, 1)}\n\n"
        f"估值日期       {html.escape(snapshot.valuation_date or '暂无')}\n"
        f"国债日期       {html.escape(snapshot.cn10y_date or '暂无')}\n"
        f"价格日期       {html.escape(snapshot.technical_price_date or '暂无')}\n\n"
        f"模式           <code>{html.escape(_display_label(SCORING_MODE_LABELS, snapshot.scoring_mode))}</code>\n"
        f"数据           <code>{html.escape(_display_label(DATA_QUALITY_LABELS, snapshot.data_quality))}</code>\n\n"
        f"触发原因：{html.escape(trigger)}\n"
        f"使用 /opcheck {snapshot.rule_id} 查看完整详情。"
    )


def format_opportunity_chunks(
    snapshot: OpportunitySnapshot,
    max_len: int = 3800,
    alert_reason: Optional[str] = None,
) -> list[str]:
    return split_message(
        format_opportunity_detail(snapshot, alert_reason=alert_reason),
        max_len=max_len,
    )
