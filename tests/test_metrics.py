import pandas as pd
import pytest

from src.metrics import (
    calculate_52w_drawdown,
    calculate_52w_high,
    calculate_ma200,
    calculate_ma200_deviation,
    calculate_percentile,
    score_drawdown,
    score_ma200,
    score_rsi,
)


def test_ma200_requires_200_valid_closes():
    assert calculate_ma200([1] * 199) is None
    assert calculate_ma200([1] * 200) == 1


def test_ma200_and_deviation_boundaries():
    assert calculate_ma200([10] * 200) == 10
    assert calculate_ma200_deviation(10, 10) == 0
    assert score_ma200(0.10) == 2
    assert score_ma200(0.05) == 2
    assert score_ma200(0) == 5
    assert score_ma200(-0.05) == 10
    assert score_ma200(-0.10) == 20


def test_52_week_requires_252_valid_closes():
    assert calculate_52w_high([1] * 251) is None
    assert calculate_52w_high([1] * 252) == 1
    assert calculate_52w_drawdown(80, 100) == pytest.approx(0.2)


def test_drawdown_boundaries():
    assert score_drawdown(0.0499) == 0
    assert score_drawdown(0.05) == 2
    assert score_drawdown(0.10) == 4
    assert score_drawdown(0.15) == 6
    assert score_drawdown(0.20) == 8
    assert score_drawdown(0.25) == 10


def test_rsi_30_belongs_to_30_40_bucket():
    assert score_rsi(70) == 0
    assert score_rsi(60) == 2
    assert score_rsi(50) == 4
    assert score_rsi(40) == 8
    assert score_rsi(30) == 12
    assert score_rsi(20) == 16
    assert score_rsi(19.99) == 20


def test_percentile_direction_and_ties():
    assert calculate_percentile(6, [3, 4, 5, 6]) == 1
    assert calculate_percentile(3, [3, 4, 5, 6]) == 0.25
    assert calculate_percentile(5, [3, 4, 5, 5, 6]) == 0.7


def test_nan_closes_are_not_valid_observations():
    closes = pd.Series([1] * 199 + [float("nan"), 1])
    assert calculate_ma200(closes) == 1
