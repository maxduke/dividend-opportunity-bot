# ADR 0004: Require adjusted prices for Opportunity scoring

## Decision

Dividend distributions mechanically change raw ETF prices. Opportunity
technical metrics therefore require adjusted price history. Raw ETF fallback
data may be used for display/provider diagnostics, but must not contribute to
RSI, MA200, or drawdown scores. If realtime raw-to-adjusted conversion is
unavailable, use the latest confirmed adjusted historical close and report
degraded quality.
