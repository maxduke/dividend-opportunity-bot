from src.metrics import classify_opportunity_level, score_dividend_bond_spread, score_dividend_yield, total_score


def test_absolute_valuation_boundaries():
    assert score_dividend_yield(3.49) == 0
    assert score_dividend_yield(3.5) == 6
    assert score_dividend_yield(4.0) == 12
    assert score_dividend_yield(5.5) == 30
    assert score_dividend_bond_spread(1.49) == 0
    assert score_dividend_bond_spread(1.5) == 4
    assert score_dividend_bond_spread(3.5) == 20


def test_percentile_scores_keep_fixed_maxima():
    assert score_dividend_yield(5, 0.9) == 27
    assert score_dividend_bond_spread(3, 0.9) == 18
    assert total_score(30, 20, 20, 20, 20) == 100


def test_valuation_gate_caps_missing_or_weak_valuation_at_watch():
    assert classify_opportunity_level(100, valuation_available=False) == "WATCH"
    assert classify_opportunity_level(100, valuation_available=True, valuation_score=19.99) == "WATCH"
    assert classify_opportunity_level(100, valuation_available=True, valuation_score=20) == "RARE"
    assert classify_opportunity_level(100, valuation_available=True, stale_valuation=True) == "WATCH"
