# Separate tradable assets from valuation benchmarks

Opportunity monitoring keeps the tradable asset and its valuation benchmark as separate domain objects: technical metrics use the asset price, while dividend valuation uses the benchmark index. This prevents ETF premiums, discounts, and fund distributions from contaminating index valuation, and lets multiple assets share one benchmark's valuation history.

The existing RSI rules remain a separate legacy path so the new model can be introduced without changing or recreating existing monitors.

