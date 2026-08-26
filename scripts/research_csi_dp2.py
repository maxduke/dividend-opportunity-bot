#!/usr/bin/env python3
"""Extract and validate the isolated historical CSI D/P2 Xueqiu archive."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.csi_dp2.models import ParseFailure, source_hash
from research.csi_dp2.parser import (
    is_candidate_post,
    merge_post_detail,
    needs_detail_request,
    parse_post,
    timeline_item_to_raw_post,
)
from research.csi_dp2.report import write_reports
from research.csi_dp2.validation import validate_archive
from research.csi_dp2.xueqiu_client import (
    NotFoundError,
    RequestError,
    XueqiuClient,
    XueqiuClientError,
)

DEFAULT_USER_ID = "8374048440"
DEFAULT_COOKIE_FILE = "~/.config/dividend-opportunity-bot/xueqiu.cookie"
BENCHMARKS = {"000922": "中证红利"}
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research the official Xueqiu archive for historical CSI D/P2 observations."
    )
    parser.add_argument("--benchmark", default="000922")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--cookie-file", default=DEFAULT_COOKIE_FILE)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--request-interval", type=float, default=2.0)
    parser.add_argument("--raw-cache-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offline", action="store_true")
    direct = parser.add_mutually_exclusive_group()
    direct.add_argument("--direct-csi-check", dest="direct_csi_check", action="store_true")
    direct.add_argument("--no-direct-csi-check", dest="direct_csi_check", action="store_false")
    parser.set_defaults(direct_csi_check=False)
    parser.add_argument("--verbose", action="store_true")
    return parser


def fetch_direct_csi(benchmark_code: str):
    """Fetch the current CSI rolling D/P2 window without importing DB-bound runtime code."""

    import akshare as ak
    import pandas as pd

    frame = ak.stock_zh_index_value_csindex(symbol=benchmark_code)
    if frame is None or frame.empty or not {"日期", "股息率2"}.issubset(frame.columns):
        raise RuntimeError("direct CSI response is empty or missing 日期/股息率2")
    dates = pd.to_datetime(frame["日期"], errors="coerce").dt.date
    yields = pd.to_numeric(
        frame["股息率2"].astype(str).str.strip().str.replace("%", "", regex=False),
        errors="coerce",
    )
    return [
        {"valuation_date": day, "dividend_yield2": float(value)}
        for day, value in zip(dates, yields)
        if pd.notna(day) and pd.notna(value)
    ]


def _raw_failure(payload, reason: str) -> ParseFailure:
    post_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
    return ParseFailure(
        post_id=post_id,
        post_url=f"https://xueqiu.com/{DEFAULT_USER_ID}/{post_id}" if post_id else "",
        post_created_at=None,
        raw_hash=source_hash(payload),
        reason=reason,
    )


def run(args: argparse.Namespace) -> tuple[str, Path]:
    if args.refresh and args.offline:
        raise ValueError("--refresh and --offline cannot be used together")
    benchmark_code = str(args.benchmark)
    if benchmark_code not in BENCHMARKS:
        raise ValueError("this research pipeline currently supports only benchmark 000922")
    if args.direct_csi_check and args.offline:
        raise ValueError("--direct-csi-check cannot be used with --offline")

    cache_dir = Path(args.raw_cache_dir or f"research/cache/xueqiu/{args.user_id}").expanduser()
    output_dir = Path(args.output_dir or f"research/output/{benchmark_code}").expanduser()
    cookie_file = None if args.offline else Path(args.cookie_file).expanduser()
    observations = []
    failures = []
    candidate_posts = 0

    with XueqiuClient(
        cookie_file=cookie_file,
        cache_dir=cache_dir,
        user_id=str(args.user_id),
        count=args.count,
        request_interval=args.request_interval,
        offline=args.offline,
        refresh=args.refresh,
    ) as client:
        timeline = client.fetch_timeline(
            start_page=args.start_page,
            max_pages=args.max_pages,
        )
        detail_circuit_open = False
        for payload in timeline.statuses:
            try:
                post = timeline_item_to_raw_post(payload, user_id=str(args.user_id))
            except (TypeError, ValueError) as exc:
                failures.append(_raw_failure(payload, f"invalid_timeline_post:{type(exc).__name__}"))
                continue
            if not is_candidate_post(
                post,
                benchmark_code=benchmark_code,
                benchmark_name=BENCHMARKS[benchmark_code],
            ):
                continue
            candidate_posts += 1
            timeline_text_complete = payload.get("truncated") is False
            if not timeline_text_complete and not detail_circuit_open and needs_detail_request(
                post,
                benchmark_code=benchmark_code,
                benchmark_name=BENCHMARKS[benchmark_code],
            ):
                try:
                    post = merge_post_detail(
                        post,
                        client.fetch_detail(post.post_id),
                        user_id=str(args.user_id),
                    )
                except NotFoundError as exc:
                    logger.warning("Post %s detail unavailable: %s", post.post_id, exc)
                except RequestError as exc:
                    detail_circuit_open = True
                    logger.warning(
                        "Post %s detail unavailable: %s; detail circuit breaker opened, "
                        "remaining detail requests skipped",
                        post.post_id,
                        exc,
                    )
                except XueqiuClientError as exc:
                    logger.warning("Post %s detail unavailable: %s", post.post_id, exc)
            parsed = parse_post(
                post,
                benchmark_code=benchmark_code,
                benchmark_name=BENCHMARKS[benchmark_code],
            )
            if parsed is None:
                continue
            if isinstance(parsed, ParseFailure):
                failures.append(parsed)
            else:
                observations.append(parsed)

    direct_csi = fetch_direct_csi(benchmark_code) if args.direct_csi_check else None
    result = validate_archive(
        observations,
        direct_csi=direct_csi,
        direct_check_requested=args.direct_csi_check,
        parse_failures=failures,
        analysis_complete=not detail_circuit_open,
    )
    write_reports(
        output_dir,
        result,
        pages=timeline.pages,
        raw_posts=timeline.raw_post_count,
        candidate_posts=candidate_posts,
        stop_reason=timeline.stop_reason,
    )
    return result.decision, output_dir


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        decision, output_dir = run(args)
    except KeyboardInterrupt:
        print("Interrupted; cached responses were retained. Re-run to continue.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, XueqiuClientError) as exc:
        logger.error("CSI D/P2 research failed: %s", exc)
        return 1
    print(f"decision={decision}")
    print(f"output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
