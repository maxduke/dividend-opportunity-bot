# CSI D/P2 archive research

This directory contains an isolated, manual research pipeline for deciding whether posts from the official `中证红利ETF` Xueqiu account form a trustworthy historical archive of CSI benchmark `000922` calculation-share dividend yield (`股息率2`, or D/P2).

D/P2 matters because CSI's calculation-share basis reflects the index weighting method. A generic market-cap-weighted dividend yield, chart OCR, constituent reconstruction, PB value, or a post-provided China 10-year bond yield is not a substitute. Historical observations retain the provenance `csindex_via_official_etf_archive`; they are not labelled as direct CSI downloads.

## Security and source limitations

Xueqiu's timeline endpoint is an undocumented, anti-crawler private API. This extractor is for one-time/manual historical research, not a production runtime dependency. It uses sequential, paced requests and a bounded retry policy, and may stop working if Xueqiu changes the endpoint.

Store only the raw Cookie header value outside the repository:

```bash
mkdir -p ~/.config/dividend-opportunity-bot
chmod 700 ~/.config/dividend-opportunity-bot
$EDITOR ~/.config/dividend-opportunity-bot/xueqiu.cookie
chmod 600 ~/.config/dividend-opportunity-bot/xueqiu.cookie
```

Never paste the cookie itself into a command, log, issue, test fixture, or CI secret. The client does not print request headers or write authentication data into cache/report files.

## Fetch and replay

Fetch the complete timeline unless a terminal condition is reached:

```bash
python scripts/research_csi_dp2.py \
  --benchmark 000922 \
  --cookie-file ~/.config/dividend-opportunity-bot/xueqiu.cookie \
  --output-dir research/output/000922
```

Re-run parsing and validation without any network access:

```bash
python scripts/research_csi_dp2.py \
  --benchmark 000922 \
  --offline \
  --raw-cache-dir research/cache/xueqiu/8374048440 \
  --output-dir research/output/000922
```

`--refresh` ignores existing cached payloads. `--direct-csi-check` explicitly enables a live AKShare/CSI overlap comparison; it is disabled by default and cannot be used with `--offline`. Timeline payloads are used first. A post detail request is made only for a potentially relevant post whose timeline text lacks a required date or yield.

Detail requests use `https://api.xueqiu.com/statuses/show.json`. HTTP 400, 404, 405, and 410 leave a `post-ID.statuses-show.not-found` endpoint-specific negative-cache marker. Later online and offline runs reuse that marker without requesting the unavailable post again; `--refresh` retries it and removes the marker after a successful response. If a detail request exhausts its transient retries, a circuit breaker skips later detail requests for that run while still parsing the cached timeline conservatively. The report then fails `TECHNICAL_COMPLETENESS`; re-run later as a new cooldown window. If the run is interrupted with Ctrl-C, fetched cache files are retained and the same command can be rerun to continue.

## Research container

The separately tagged research image defaults to this CLI rather than the Telegram bot. Run it as the host user so a mode-`600` cookie remains readable without loosening permissions, and mount the cookie read-only:

```bash
mkdir -p research-run/cache research-run/output

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v ~/.config/dividend-opportunity-bot/xueqiu.cookie:/run/secrets/xueqiu.cookie:ro \
  -v "$PWD/research-run/cache:/work/cache" \
  -v "$PWD/research-run/output:/work/output" \
  ghcr.io/maxduke/dividend-opportunity-bot:RESEARCH_TAG \
  --cookie-file /run/secrets/xueqiu.cookie \
  --raw-cache-dir /work/cache \
  --output-dir /work/output \
  --direct-csi-check
```

The production image, compose file, default command, and `latest` tag are unchanged.

## Outputs and decision

Generated cache and output directories are gitignored. The output directory contains parsed observations, HIGH-basis observations, identical duplicates, conflicts, parse failures, missing intervals, deterministic JSON, and a Markdown summary.

The report issues exactly one decision: `ELIGIBLE_FOR_BACKFILL` or `NOT_ELIGIBLE_FOR_BACKFILL`. Eligibility requires a completed direct-CSI overlap check, at least 95% HIGH-basis proposed rows, exact agreement for every overlap date, no conflicts, matching available exact checkpoints, at least 252 unique HIGH observations across 2.0 years, at least 70% HIGH-session coverage with no materially represented year below 50%, and no unexplained gap over 20 XSHG sessions. A year is materially represented when at least 60 expected sessions fall inside the measured archive range. MEDIUM rows do not fill HIGH-confidence gaps.

The 2025-01-17 六亿居士 value is retained only as an approximate Tier-4 sanity checkpoint. It validates an archive row when present but is never emitted as an observation or used to establish authoritative basis.

Eligibility means only that a later, separately reviewed importer may be designed. This code never imports the production database layer, opens `DB_FILE`, writes SQLite, changes scoring, or enables percentile mode.
