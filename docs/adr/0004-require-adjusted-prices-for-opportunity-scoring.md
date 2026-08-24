# ADR 0004: Require adjusted prices for Opportunity scoring

## Decision

Opportunity technical history uses qfq. Qfq keeps the current market price
unchanged and adjusts historical prices, so the current raw realtime quote is
the current qfq anchor. Never multiply today's raw quote by a historical
qfq/raw factor.

A raw realtime quote may be merged only with qfq history whose adjustment
basis is confirmed current for the Shanghai date. If the qfq basis is
stale/unconfirmed, or the quote timestamp is unavailable, use the latest
confirmed qfq historical close and mark the result degraded.

Raw ETF fallback data may be used for display/provider diagnostics, but must
not contribute to RSI, MA200, or drawdown scores. Sina
``stock_zh_a_minute(adjust=qfq)`` must not be relied upon for ETF qfq realtime
data.
