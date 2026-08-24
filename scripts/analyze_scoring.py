#!/usr/bin/env python3
"""Replay the frozen V1 Opportunity score against local history.

The replay is deliberately a report, not an optimizer.  It reads valuation
and China 10Y rows from SQLite and fetches the ETF's adjusted history through
the normal provider layer.  Every value used to calculate a score is bounded
by the replay date; forward returns are added only after that calculation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

# ``python scripts/analyze_scoring.py`` does not put the repository root on
# ``sys.path``; make the documented CLI work without requiring installation.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as runtime_config
from src.config import (
    CSI_DIVIDEND_YIELD_FIELD,
    MIN_VALUATION_SCORE_FOR_OPPORTUNITY,
    RSI_PERIOD,
    TECHNICAL_HISTORY_DAYS,
    VALUATION_PERCENTILE_LOOKBACK_YEARS,
    VALUATION_PERCENTILE_MIN_OBS,
)
from src.metrics import (
    calculate_52w_drawdown,
    calculate_52w_high,
    calculate_ma200,
    calculate_ma200_deviation,
    calculate_percentile,
    classify_opportunity_level,
    score_dividend_bond_spread,
    score_dividend_yield,
    score_drawdown,
    score_ma200,
    score_rsi,
    total_score,
)
from src.scoring_config import OPPORTUNITY_LEVELS

HORIZONS = (21, 63, 126, 252)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRICE_ADJUSTMENTS = ("hfq", "qfq")
COMPONENT_COLUMNS = (
    "dividend_yield_score",
    "spread_score",
    "ma200_score",
    "drawdown_score",
    "rsi_score",
)
COMPONENT_MAXIMUMS = {
    "dividend_yield_score": 30,
    "spread_score": 20,
    "ma200_score": 20,
    "drawdown_score": 10,
    "rsi_score": 20,
}


def _as_date(value: Any) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(str(value).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) and number == number and abs(number) != float("inf") else None


def _field(row: Any, *names: str) -> Any:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    for name in names:
        if name in keys:
            return row[name]
    return None


def _records(rows: Any) -> list[Any]:
    if rows is None:
        return []
    if isinstance(rows, pd.DataFrame):
        return rows.to_dict("records")
    return list(rows)


def _normalise_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted ``date``/``close`` frame and reject bad observations."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "close"])
    attrs = dict(getattr(frame, "attrs", {}))
    result = frame.copy()
    close_column = next(
        (name for name in ("收盘", "close", "Close", "adj_close", "adjusted_close") if name in result.columns),
        None,
    )
    if close_column is None:
        raise ValueError("adjusted history has no close column")
    date_column = next(
        (name for name in ("日期", "date", "Date", "datetime") if name in result.columns),
        None,
    )
    dates = result[date_column] if date_column else pd.Series(result.index, index=result.index)
    parsed_dates = pd.Series(pd.to_datetime(dates, errors="coerce"), index=result.index)
    if getattr(parsed_dates.dt, "tz", None) is not None:
        parsed_dates = parsed_dates.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    result = pd.DataFrame({"date": parsed_dates.dt.date, "close": pd.to_numeric(result[close_column], errors="coerce")})
    result = result.dropna(subset=["date", "close"])
    result = result[result["close"] > 0]
    result = result.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    result.attrs.update(attrs)
    return result


def _normalise_valuations(rows: Any) -> list[dict[str, Any]]:
    result = []
    for row in _records(rows):
        row_date = _as_date(_field(row, "valuation_date", "日期", "date"))
        if row_date is None:
            continue
        result.append(
            {
                "date": row_date,
                "dividend_yield1": _number(_field(row, "dividend_yield1", "股息率1")),
                "dividend_yield2": _number(_field(row, "dividend_yield2", "股息率2")),
                "benchmark_name": _field(row, "benchmark_name", "指数名称", "指数中文简称"),
            }
        )
    return sorted(result, key=lambda row: row["date"])


def _normalise_bonds(rows: Any) -> list[dict[str, Any]]:
    result = []
    for row in _records(rows):
        row_date = _as_date(_field(row, "yield_date", "日期", "date"))
        value = _number(_field(row, "cn10y", "10年", "close", "收盘"))
        if row_date is not None and value is not None:
            result.append({"date": row_date, "cn10y": value, "source": _field(row, "source")})
    return sorted(result, key=lambda row: row["date"])


def _years_before(value: date, years: int) -> date:
    return (pd.Timestamp(value) - pd.DateOffset(years=years)).date()


def _latest_on_or_before(rows: list[dict[str, Any]], target: date) -> Optional[dict[str, Any]]:
    candidates = [row for row in rows if row["date"] <= target]
    return max(candidates, key=lambda row: row["date"]) if candidates else None


def _match_bond(rows: list[dict[str, Any]], target: date, max_gap_days: int = 7) -> Optional[dict[str, Any]]:
    matched = _latest_on_or_before(rows, target)
    return matched if matched is not None and (target - matched["date"]).days <= max_gap_days else None


def _dated_spreads(
    valuations: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    field: str,
    end_date: date,
    lookback_years: int,
) -> list[tuple[date, float]]:
    cutoff = _years_before(end_date, lookback_years)
    values = []
    for valuation in valuations:
        valuation_date = valuation["date"]
        if not cutoff <= valuation_date <= end_date:
            continue
        dividend_yield = valuation.get(field)
        bond = _match_bond(bonds, valuation_date)
        if dividend_yield is not None and bond is not None:
            values.append((valuation_date, dividend_yield - bond["cn10y"]))
    return values


def _history_is_mature(values: list[tuple[date, float]], minimum_observations: int, minimum_span_years: float) -> bool:
    if len(values) < minimum_observations:
        return False
    dates = [value_date for value_date, _ in values]
    return (max(dates) - min(dates)).days / 365.0 >= minimum_span_years


def _classify_level(
    score: float,
    *,
    valuation_available: bool,
    valuation_score: float,
    stale_valuation: bool,
    scoring_mode: str,
) -> str:
    return classify_opportunity_level(
        score,
        valuation_available=valuation_available,
        valuation_score=valuation_score,
        stale_valuation=stale_valuation,
        min_valuation_score=MIN_VALUATION_SCORE_FOR_OPPORTUNITY,
        scoring_mode=scoring_mode,
    )


def _stale_by_price_sessions(valuation_date: date, current_date: date, price_dates: list[date], maximum: int) -> bool:
    """Count sessions from the adjusted ETF rows, avoiding a replay network call."""
    sessions = sum(valuation_date < value_date <= current_date for value_date in price_dates)
    return sessions > maximum


def replay_scores(
    prices: pd.DataFrame,
    valuations: Iterable[Any],
    bonds: Iterable[Any],
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    rsi_period: int = RSI_PERIOD,
    minimum_observations: int = VALUATION_PERCENTILE_MIN_OBS,
    minimum_span_years: float = float(getattr(runtime_config, "VALUATION_PERCENTILE_MIN_SPAN_YEARS", 2.0)),
    lookback_years: int = VALUATION_PERCENTILE_LOOKBACK_YEARS,
    stale_max_trading_days: int = int(getattr(runtime_config, "VALUATION_STALE_MAX_TRADING_DAYS", 3)),
) -> pd.DataFrame:
    """Calculate one score per price date with no look-ahead data access."""
    price_rows = _normalise_price_frame(prices)
    valuation_rows = _normalise_valuations(valuations)
    bond_rows = _normalise_bonds(bonds)
    field = "dividend_yield1" if CSI_DIVIDEND_YIELD_FIELD == "股息率1" else "dividend_yield2"
    price_dates = price_rows["date"].tolist()
    output: list[dict[str, Any]] = []

    for index, current_row in price_rows.iterrows():
        current_date = current_row["date"]
        if end and current_date > end:
            break
        if start and current_date < start:
            continue
        # A replay cannot invent a valuation before the first locally persisted row.
        valuation = _latest_on_or_before(valuation_rows, current_date)
        if valuation is None:
            continue
        valuation_date = valuation["date"]
        dividend_yield = valuation.get(field)
        valuation_available = dividend_yield is not None
        bond = _match_bond(bond_rows, valuation_date)
        cn10y = bond["cn10y"] if bond else None
        spread = dividend_yield - cn10y if dividend_yield is not None and cn10y is not None else None

        prefix = price_rows.iloc[: index + 1]["close"]
        ma200 = calculate_ma200(prefix)
        ma_deviation = calculate_ma200_deviation(float(current_row["close"]), ma200)
        high_52w = calculate_52w_high(prefix)
        drawdown = calculate_52w_drawdown(float(current_row["close"]), high_52w)
        try:
            from src.data_fetcher import calculate_rsi_wilder

            rsi = calculate_rsi_wilder(prefix, period=rsi_period)
        except (ImportError, TypeError):
            rsi = None

        cutoff = _years_before(valuation_date, lookback_years)
        dy_history = [
            (row["date"], row[field])
            for row in valuation_rows
            if cutoff <= row["date"] <= valuation_date and row.get(field) is not None
        ]
        spread_history = _dated_spreads(
            valuation_rows, bond_rows, field, valuation_date, lookback_years
        )
        dy_mature = _history_is_mature(dy_history, minimum_observations, minimum_span_years)
        spread_mature = _history_is_mature(spread_history, minimum_observations, minimum_span_years)
        dy_percentile = calculate_percentile(dividend_yield, [value for _, value in dy_history]) if dy_mature else None
        spread_percentile = calculate_percentile(spread, [value for _, value in spread_history]) if spread_mature else None

        dividend_yield_score = score_dividend_yield(dividend_yield, dy_percentile)
        spread_score = score_dividend_bond_spread(spread, spread_percentile)
        ma200_score = score_ma200(ma_deviation)
        drawdown_score = score_drawdown(drawdown)
        rsi_score = score_rsi(rsi)
        valuation_score = total_score(dividend_yield_score, spread_score)
        long_term_score = total_score(ma200_score, drawdown_score)
        tactical_score = total_score(rsi_score)
        score = total_score(valuation_score, long_term_score, tactical_score)
        if not valuation_available:
            scoring_mode = "NONE"
        elif dy_percentile is not None and spread_percentile is not None:
            scoring_mode = "PERCENTILE"
        elif dy_percentile is None and spread_percentile is None:
            scoring_mode = "ABSOLUTE_FALLBACK"
        else:
            scoring_mode = "MIXED"
        stale = _stale_by_price_sessions(
            valuation_date, current_date, price_dates, stale_max_trading_days
        )
        level = _classify_level(
            score,
            valuation_available=valuation_available,
            valuation_score=valuation_score,
            stale_valuation=stale,
            scoring_mode=scoring_mode,
        )
        output.append(
            {
                "date": current_date,
                "price": float(current_row["close"]),
                "valuation_date": valuation_date,
                "cn10y_date": bond["date"] if bond else None,
                "cn10y_source": bond.get("source") if bond else None,
                "dividend_yield_used": dividend_yield,
                "cn10y": cn10y,
                "dividend_bond_spread": spread,
                "dividend_yield_percentile": dy_percentile,
                "spread_percentile": spread_percentile,
                "dividend_yield_score": dividend_yield_score,
                "spread_score": spread_score,
                "ma200_score": ma200_score,
                "drawdown_score": drawdown_score,
                "rsi_score": rsi_score,
                "valuation_score": valuation_score,
                "long_term_score": long_term_score,
                "tactical_score": tactical_score,
                "total_score": score,
                "score": score,
                "level": level,
                "scoring_mode": scoring_mode,
                "stale_valuation": stale,
                "ma200_deviation": ma_deviation,
                "drawdown_52w": drawdown,
                "rsi6": rsi,
            }
        )

    result = pd.DataFrame(output)
    result.attrs["valuation_percentile_min_obs"] = minimum_observations
    result.attrs["valuation_percentile_min_span_years"] = minimum_span_years
    if result.empty:
        return result
    result = result.sort_values("date").reset_index(drop=True)
    for horizon in HORIZONS:
        result[f"forward_{horizon}d"] = result["price"].shift(-horizon) / result["price"] - 1
    return result


def load_local_history(db_path: str, benchmark: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read only the persisted valuation and bond tables; never writes SQLite."""
    if db_path == ":memory:":
        connection = sqlite3.connect(db_path)
    else:
        path = Path(db_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"database not found: {path}")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        valuations = connection.execute(
            "SELECT * FROM benchmark_valuation_snapshots WHERE benchmark_code = ? ORDER BY valuation_date",
            (str(benchmark),),
        ).fetchall()
        bonds = connection.execute(
            "SELECT * FROM macro_yield_snapshots ORDER BY yield_date"
        ).fetchall()
        return list(valuations), list(bonds)
    finally:
        connection.close()


async def _fetch_adjusted_history(asset: str, days: int, price_adjust: str = "hfq") -> pd.DataFrame:
    if price_adjust not in PRICE_ADJUSTMENTS:
        raise ValueError(f"unsupported replay price adjustment: {price_adjust}")
    from src.provider_bootstrap import install_data_provider_patch

    install_data_provider_patch()
    from src.data_fetcher import get_history_data

    frame = await get_history_data(asset, days, price_adjust=price_adjust)
    if frame is None or frame.empty:
        raise RuntimeError(f"no {price_adjust} ETF history returned for {asset}")
    if frame.attrs.get("price_basis") != price_adjust:
        raise RuntimeError(
            f"provider did not confirm {price_adjust} history; replay aborted"
        )
    return frame


def fetch_adjusted_history(
    asset: str,
    start: Optional[date],
    end: Optional[date],
    earliest_valuation: Optional[date],
    price_adjust: str = "hfq",
) -> pd.DataFrame:
    today = datetime.now(SHANGHAI_TZ).date()
    requested_start = start or earliest_valuation or (today - timedelta(days=365 * 5))
    fetch_start = requested_start - timedelta(days=TECHNICAL_HISTORY_DAYS)
    days = max(TECHNICAL_HISTORY_DAYS, (today - fetch_start).days + 1)
    frame = asyncio.run(_fetch_adjusted_history(asset, days, price_adjust))
    normalised = _normalise_price_frame(frame)
    if end is not None:
        normalised = normalised[normalised["date"] <= end]
        normalised.attrs.update(frame.attrs)
    return normalised.reset_index(drop=True)


def _parse_cli_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def print_report(result: pd.DataFrame, price_adjust: str = "hfq") -> None:
    print(f"Replay technical price basis: {price_adjust.upper()}")
    if price_adjust == "qfq":
        print("WARNING: qfq historical prices are restated after corporate actions")
    if result.empty:
        print("Available period: none")
        print("Observations: 0")
        print("\nScoring mode distribution:")
        for mode in ("PERCENTILE", "MIXED", "ABSOLUTE_FALLBACK", "NONE"):
            print(f"{mode:<18} {0:>6}")
        print("WARNING: no mature PERCENTILE observations are available.")
        print("Do not use this replay to tune V1 weights or thresholds yet.")
        print(
            "Available valuation history does not satisfy the configured maturity rules: "
            f">= {VALUATION_PERCENTILE_MIN_OBS} observations and "
            f">= {float(getattr(runtime_config, 'VALUATION_PERCENTILE_MIN_SPAN_YEARS', 2.0)):g} years of span."
        )
        return
    first, last = result["date"].min(), result["date"].max()
    print(f"Available period: {first} → {last}")
    print(f"Observations: {len(result)}")
    print("\nScore distribution:")
    counts = result["level"].value_counts()
    for _, level, _ in OPPORTUNITY_LEVELS:
        count = int(counts.get(level, 0))
        print(f"{level:<10} {count:>6} {count / len(result) * 100:>6.1f}%")

    print("\nScoring mode distribution:")
    modes = ("PERCENTILE", "MIXED", "ABSOLUTE_FALLBACK", "NONE")
    mode_counts = result["scoring_mode"].value_counts()
    for mode in modes:
        print(f"{mode:<18} {int(mode_counts.get(mode, 0)):>6}")
    if not int(mode_counts.get("PERCENTILE", 0)):
        minimum_observations = result.attrs.get(
            "valuation_percentile_min_obs", VALUATION_PERCENTILE_MIN_OBS
        )
        minimum_span_years = result.attrs.get(
            "valuation_percentile_min_span_years",
            float(getattr(runtime_config, "VALUATION_PERCENTILE_MIN_SPAN_YEARS", 2.0)),
        )
        print("WARNING: no mature PERCENTILE observations are available.")
        print("Do not use this replay to tune V1 weights or thresholds yet.")
        print(
            "Available valuation/spread history does not satisfy the configured maturity rules: "
            f">= {minimum_observations} observations and "
            f">= {minimum_span_years:g} years of span."
        )
    print("\nScore quantiles:")
    for label, quantile in (("p10", .10), ("p25", .25), ("median", .50), ("p75", .75), ("p90", .90), ("p95", .95)):
        print(f"{label:<7} {result['score'].quantile(quantile):.2f}")
    print(f"{'max':<7} {result['score'].max():.2f}")

    print("\nComponent correlations (Pearson / Spearman):")
    pearson = result[list(COMPONENT_COLUMNS)].corr(method="pearson")
    spearman = result[list(COMPONENT_COLUMNS)].rank().corr(method="pearson")
    print("Pearson:")
    print(pearson.to_string(float_format=lambda value: f"{value:.3f}"))
    print("Spearman:")
    print(spearman.to_string(float_format=lambda value: f"{value:.3f}"))
    pairs = (
        ("DY score", "dividend_yield_score", "Spread score", "spread_score"),
        ("MA score", "ma200_score", "Drawdown score", "drawdown_score"),
        ("Long-Term score", "long_term_score", "RSI score", "rsi_score"),
    )
    for left_label, left, right_label, right in pairs:
        pair = result[[left, right]].dropna()
        if len(pair) < 2 or pair[left].nunique() < 2 or pair[right].nunique() < 2:
            pearson_value = spearman_value = float("nan")
        else:
            pearson_value = pair.corr().loc[left, right]
            spearman_value = pair.rank().corr().loc[left, right]
        print(
            f"corr({left_label}, {right_label}) = "
            f"{pearson_value:.3f} / {spearman_value:.3f}"
        )

    print("\nLevel forward performance:")
    header = "Level       N" + "".join(f"  +{h}d median  +{h}d positive%" for h in HORIZONS)
    print(header)
    for _, level, _ in OPPORTUNITY_LEVELS:
        subset = result[result["level"] == level]
        values = [f"{level:<10} {len(subset):>3}"]
        for horizon in HORIZONS:
            forward = subset[f"forward_{horizon}d"].dropna()
            values.append(f"{forward.median() * 100:>10.2f}% {forward.gt(0).mean() * 100:>8.1f}%" if len(forward) else f"{'N/A':>10} {'N/A':>8}")
        print(" ".join(values))

    print("\nThreshold frequency:")
    for threshold in (60, 75, 85):
        count = int(result["score"].ge(threshold).sum())
        print(f"score >= {threshold}: {count} ({count / len(result) * 100:.1f}%)")

    print("\nComponent sensitivity:")
    for column in COMPONENT_COLUMNS:
        values = result[column]
        maximum = COMPONENT_MAXIMUMS[column]
        print(
            f"{column}: mean={values.mean():.2f}, median={values.median():.2f}, "
            f"std={values.std():.2f}, zero={values.eq(0).mean() * 100:.1f}%, "
            f"max={values.eq(maximum).mean() * 100:.1f}%"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay frozen V1 Dividend Opportunity scoring")
    parser.add_argument("--asset", required=True, help="ETF/asset code, e.g. 515180")
    parser.add_argument("--benchmark", required=True, help="CSI benchmark code, e.g. 000922")
    parser.add_argument("--start", type=_parse_cli_date)
    parser.add_argument("--end", type=_parse_cli_date)
    parser.add_argument(
        "--price-adjust",
        choices=PRICE_ADJUSTMENTS,
        default="hfq",
        help="historical replay price basis (default: hfq)",
    )
    parser.add_argument("--db", default=os.getenv("DB_FILE", "rules.db"))
    parser.add_argument("--csv", help="write replay rows to this CSV path")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.start and args.end and args.start > args.end:
        print("--start must not be after --end", file=sys.stderr)
        return 2
    try:
        valuation_rows, bond_rows = load_local_history(args.db, args.benchmark)
        normalised_valuations = _normalise_valuations(valuation_rows)
        if not normalised_valuations:
            print_report(pd.DataFrame(), args.price_adjust)
            return 0
        earliest = normalised_valuations[0]["date"]
        prices = fetch_adjusted_history(
            args.asset,
            args.start,
            args.end,
            earliest,
            args.price_adjust,
        )
        result = replay_scores(
            prices,
            valuation_rows,
            bond_rows,
            start=args.start,
            end=args.end,
        )
        if args.csv:
            result.to_csv(args.csv, index=False)
        print_report(result, args.price_adjust)
        return 0
    except (FileNotFoundError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
