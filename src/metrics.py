"""Pure opportunity metrics and scoring functions."""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from .scoring_config import (
    DIVIDEND_YIELD_BUCKETS,
    DRAWDOWN_BUCKETS,
    MA200_BUCKETS,
    OPPORTUNITY_LEVELS,
    RSI_BUCKETS,
    SPREAD_BUCKETS,
)


def _valid_values(values: Iterable[float]) -> list[float]:
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def _clamp_score(score: float, maximum: float) -> float:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return round(max(0.0, min(float(maximum), value)), 2)


def _bucket_score(value: Optional[float], buckets: Sequence[tuple[float, float]], maximum: float) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    for boundary, score in buckets:
        if number < boundary:
            return float(score)
    return float(maximum)


def valid_close_count(closes: Iterable[float]) -> int:
    return len(_valid_values(closes))


def calculate_ma200(closes: Iterable[float]) -> Optional[float]:
    values = _valid_values(closes)
    if len(values) < 200:
        return None
    return float(sum(values[-200:]) / 200)


def calculate_ma200_deviation(current_price: Optional[float], ma200: Optional[float]) -> Optional[float]:
    if current_price is None or ma200 is None or ma200 == 0:
        return None
    try:
        current = float(current_price)
        average = float(ma200)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or not math.isfinite(average) or average == 0:
        return None
    return current / average - 1


def calculate_52w_high(closes: Iterable[float]) -> Optional[float]:
    values = _valid_values(closes)
    if len(values) < 252:
        return None
    return max(values[-252:])


def calculate_52w_drawdown(current_price: Optional[float], high_52w: Optional[float]) -> Optional[float]:
    if current_price is None or high_52w is None or high_52w == 0:
        return None
    try:
        current = float(current_price)
        high = float(high_52w)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or not math.isfinite(high) or high == 0:
        return None
    return 1 - current / high


def calculate_percentile(current: Optional[float], history: Iterable[float]) -> Optional[float]:
    """Return the average-rank percentile; ties receive the same midpoint rank."""
    if current is None:
        return None
    try:
        current_value = float(current)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current_value):
        return None
    values = _valid_values(history)
    if not values:
        return None
    less = sum(value < current_value for value in values)
    equal = sum(value == current_value for value in values)
    # Mid-rank for ties; for an unseen value this is the midpoint between neighbors.
    return max(0.0, min(1.0, (less + (equal + 1) / 2) / len(values)))


def score_dividend_yield(value: Optional[float], percentile: Optional[float] = None) -> float:
    if value is None:
        return 0.0
    if percentile is not None:
        return _clamp_score(float(percentile) * 30, 30)
    return _clamp_score(_bucket_score(value, DIVIDEND_YIELD_BUCKETS, 30), 30)


def score_dividend_bond_spread(value: Optional[float], percentile: Optional[float] = None) -> float:
    if value is None:
        return 0.0
    if percentile is not None:
        return _clamp_score(float(percentile) * 20, 20)
    return _clamp_score(_bucket_score(value, SPREAD_BUCKETS, 20), 20)


def score_ma200(deviation: Optional[float]) -> float:
    if deviation is None:
        return 0.0
    try:
        value = float(deviation)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    for operator, boundary, score in MA200_BUCKETS:
        if (operator == ">" and value > boundary) or (operator == ">=" and value >= boundary):
            return float(score)
    return 0.0


def score_drawdown(drawdown: Optional[float]) -> float:
    return _clamp_score(_bucket_score(drawdown, DRAWDOWN_BUCKETS, 10), 10)


def score_rsi(rsi: Optional[float]) -> float:
    if rsi is None:
        return 0.0
    try:
        value = float(rsi)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    for boundary, score in RSI_BUCKETS:
        if value >= boundary:
            return float(score)
    return 20.0


def classify_opportunity_level(
    total_score: float,
    valuation_available: bool = True,
    valuation_score: float = 50,
    stale_valuation: bool = False,
    min_valuation_score: float = 20,
) -> str:
    score = _clamp_score(total_score, 100)
    level = "NEUTRAL"
    for boundary, candidate, _ in OPPORTUNITY_LEVELS:
        if score >= boundary:
            level = candidate
    if not valuation_available or stale_valuation or valuation_score < min_valuation_score:
        if level in {"MODERATE", "STRONG", "RARE"}:
            return "WATCH"
    return level


def level_rank(level: Optional[str]) -> int:
    return {name: index for index, (_, name, _) in enumerate(OPPORTUNITY_LEVELS)}.get(level or "NEUTRAL", 0)


def is_level_upgrade(previous: Optional[str], current: Optional[str]) -> bool:
    return level_rank(current) > level_rank(previous)


def total_score(*scores: Optional[float]) -> float:
    values = []
    for score in scores:
        try:
            value = float(score or 0)
        except (TypeError, ValueError):
            value = 0
        values.append(value if math.isfinite(value) else 0)
    return _clamp_score(sum(values), 100)
