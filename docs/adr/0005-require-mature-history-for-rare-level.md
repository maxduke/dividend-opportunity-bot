# ADR 0005: Require mature history for RARE level

## Decision

Historical percentile mode requires both sufficient observations and a
minimum calendar span. Absolute fallback remains available during data
bootstrap, but RARE is reserved for fully mature percentile mode. This
prevents a new benchmark from receiving the strongest label solely from
universal absolute thresholds.
