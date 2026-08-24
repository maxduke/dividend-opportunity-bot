"""V1 scoring buckets; keep thresholds in one place.

Weights and thresholds are intentionally frozen until historical replay is
reviewed; this PR does not tune scoring.
"""

MA200_BUCKETS = (
    (">", 0.10, 0),
    (">=", 0.05, 2),
    (">=", 0.0, 5),
    (">=", -0.05, 10),
    (">", -0.10, 15),
    (">=", float("-inf"), 20),
)

DRAWDOWN_BUCKETS = (
    (0.05, 0),
    (0.10, 2),
    (0.15, 4),
    (0.20, 6),
    (0.25, 8),
)

DIVIDEND_YIELD_BUCKETS = (
    (3.5, 0),
    (4.0, 6),
    (4.5, 12),
    (5.0, 18),
    (5.5, 24),
)

SPREAD_BUCKETS = (
    (1.5, 0),
    (2.0, 4),
    (2.5, 8),
    (3.0, 12),
    (3.5, 16),
)

RSI_BUCKETS = (
    (70, 0),
    (60, 2),
    (50, 4),
    (40, 8),
    (30, 12),
    (20, 16),
)

OPPORTUNITY_LEVELS = (
    (0, "NEUTRAL", "⚪"),
    (45, "WATCH", "🟡"),
    (60, "MODERATE", "🟢"),
    (75, "STRONG", "🟢"),
    (85, "RARE", "🔥"),
)
