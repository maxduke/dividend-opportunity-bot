#!/usr/bin/env python3
"""Live AKShare smoke test; never imported by the normal CI test suite."""

import argparse
import multiprocessing as mp
import sys
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


REQUIRED_CSI_COLUMNS = ("日期", "指数代码", "股息率1", "股息率2", "市盈率1", "市盈率2")


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{name:<18} {'PASS' if ok else 'FAIL'}{': ' + detail if detail else ''}")
    return ok


def _isolated_worker(queue, operation: str, args: tuple):
    try:
        if operation.startswith("proxy_") or operation == "history":
            from src.provider_bootstrap import install_data_provider_patch

            patch_active = install_data_provider_patch()
            if operation.startswith("proxy_") and not patch_active:
                queue.put((False, "proxy patch is disabled"))
                return

        import akshare as ak

        if operation.startswith("proxy_"):
            import akshare_proxy_patch
            import requests

            from src.config import AKSHARE_PROXY_HOOK_DOMAINS

            # The third-party patch falls back to a direct request after its
            # retries. Block that path so PASS proves the proxy served the call.
            original_direct_request = requests._OriginalSession.request
            hook_domains = tuple(
                domain.strip()
                for domain in AKSHARE_PROXY_HOOK_DOMAINS.split(",")
                if domain.strip()
            )

            def reject_target_direct_fallback(session, method, url, **kwargs):
                if any(domain in (url or "") for domain in hook_domains):
                    raise RuntimeError("target request attempted direct fallback")
                return original_direct_request(session, method, url, **kwargs)

            requests._OriginalSession.request = reject_target_direct_fallback

            proxy_cache_before = akshare_proxy_patch._cache.data

            if operation == "proxy_stock_history":
                frame = ak.stock_zh_a_hist(
                    symbol=args[0],
                    period="daily",
                    start_date=args[1],
                    end_date=args[2],
                    adjust="",
                )
                valid = int(pd.to_numeric(frame.get("收盘"), errors="coerce").notna().sum())
                result = {"rows": len(frame), "valid_close": valid}
                ok = len(frame) > 252 and valid > 252
            elif operation == "proxy_etf_history":
                frame = ak.fund_etf_hist_em(
                    symbol=args[0],
                    period="daily",
                    start_date=args[1],
                    end_date=args[2],
                    adjust="",
                )
                valid = int(pd.to_numeric(frame.get("收盘"), errors="coerce").notna().sum())
                result = {"rows": len(frame), "valid_close": valid}
                ok = len(frame) > 252 and valid > 252
            elif operation == "proxy_stock_info":
                frame = ak.stock_individual_info_em(symbol=args[0])
                items = set(frame.get("item", pd.Series(dtype=str)).dropna().astype(str))
                result = {"rows": len(frame), "items": sorted(items)}
                ok = not frame.empty and "股票简称" in items
            else:
                raise ValueError(f"unknown proxy operation: {operation}")

            result["proxy_auth_fetched"] = (
                proxy_cache_before is None and akshare_proxy_patch._cache.data is not None
            )
            queue.put((ok, result))
            return

        if operation == "history":
            import asyncio

            from src.data_fetcher import get_history_data

            frame = asyncio.run(get_history_data(args[0], 550))
            valid = 0 if frame is None else int(pd.to_numeric(frame.get("收盘"), errors="coerce").notna().sum())
            queue.put((True, {"rows": 0 if frame is None else len(frame), "valid": valid}))
        elif operation == "price":
            import asyncio

            from src.data_fetcher import _fetch_single_realtime_price

            price = asyncio.run(_fetch_single_realtime_price(args[0]))
            queue.put((price is not None and float(price) > 0, price))
        elif operation == "csi":
            frame = ak.stock_zh_index_value_csindex(symbol=args[0])
            missing = [column for column in REQUIRED_CSI_COLUMNS if column not in frame.columns]
            if frame.empty or missing:
                queue.put((False, {"rows": len(frame), "missing": missing, "columns": list(frame.columns)}))
                return
            dates = pd.to_datetime(frame["日期"], errors="coerce")
            valid = dates.dropna()
            if valid.empty:
                queue.put((False, "no valid valuation date"))
                return
            latest = frame.loc[valid.idxmax()]
            queue.put((True, {
                "rows": len(frame),
                "earliest": str(valid.min().date()),
                "latest_date": str(valid.max().date()),
                "latest": {column: latest[column] for column in REQUIRED_CSI_COLUMNS[1:]},
            }))
        elif operation == "bond":
            frame = ak.bond_china_yield(start_date=args[0], end_date=args[1])
            curve = frame
            if "曲线名称" in frame.columns:
                curve = frame[frame["曲线名称"] == "中债国债收益率曲线"]
            if curve.empty or "10年" not in curve.columns:
                queue.put((False, {"columns": list(frame.columns), "rows": len(frame)}))
                return
            yields = pd.to_numeric(curve["10年"], errors="coerce").dropna()
            if yields.empty:
                queue.put((False, "no valid 10-year yield"))
                return
            latest = curve.loc[yields.index[-1]]
            queue.put((True, {"date": latest.get("日期", "unknown"), "yield": yields.iloc[-1]}))
        elif operation == "fallback":
            frame = ak.bond_gb_zh_sina(symbol="中国10年期国债")
            close_column = "close" if "close" in frame.columns else "收盘" if "收盘" in frame.columns else None
            if close_column is None:
                queue.put((False, {"columns": list(frame.columns), "rows": len(frame)}))
                return
            close = pd.to_numeric(frame[close_column], errors="coerce").dropna()
            queue.put((not close.empty, {"latest": None if close.empty else close.iloc[-1]}))
    except Exception as exc:
        queue.put((False, str(exc)))


def _isolated_call(operation: str, *args, timeout: int = 30):
    # curl_cffi sessions must not be inherited from a forked parent.
    context = mp.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_isolated_worker, args=(queue, operation, args))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(2)
        return False, f"timeout after {timeout}s"
    try:
        return queue.get(timeout=2)
    except Exception:
        return False, f"worker exited with code {process.exitcode}"


def _check_asset(asset_code: str, timeout: int) -> tuple[bool, bool]:
    history_ok, history = _isolated_call("history", asset_code, timeout=timeout)
    close_count = int(history.get("valid", 0)) if history_ok else 0
    rows = int(history.get("rows", 0)) if history_ok else 0
    history_ok = history_ok and rows > 252
    latest_ok = history_ok and close_count > 252
    _result("ETF history", history_ok, f"rows={rows}")
    _result("Valid close", latest_ok, f"valid_close={close_count}")

    price_ok, price = _isolated_call("price", asset_code, timeout=timeout)
    _result("Realtime price", price_ok, f"price={price}" if price_ok else str(price))
    return history_ok and latest_ok, price_ok


def _check_proxy_interfaces(asset_code: str, timeout: int) -> bool:
    today = date.today()
    start = (today - timedelta(days=550)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    checks = (
        ("Proxy stock history", "proxy_stock_history", ("000001", start, end)),
        ("Proxy ETF history", "proxy_etf_history", (asset_code, start, end)),
        ("Proxy stock info", "proxy_stock_info", ("000001",)),
    )
    all_ok = True
    for label, operation, args in checks:
        ok, result = _isolated_call(operation, *args, timeout=timeout)
        if not ok:
            _result(label, False, str(result))
            all_ok = False
            continue
        proxy_ok = bool(result.get("proxy_auth_fetched"))
        _result(
            label,
            ok and proxy_ok,
            f"{result}; route={'PATCH' if proxy_ok else 'DIRECT/UNKNOWN'}",
        )
        all_ok = all_ok and ok and proxy_ok
    _result(
        "ETF name endpoint",
        True,
        "SKIP: fund_name_em uses .js; patch bypasses it and proxy mode uses code fallback",
    )
    return all_ok


def _check_csi(benchmark_code: str, timeout: int) -> bool:
    ok, summary = _isolated_call("csi", benchmark_code, timeout=timeout)
    if not ok:
        return _result("CSI valuation", False, str(summary))
    latest = summary["latest"]
    dy1_ok = pd.notna(latest["股息率1"])
    dy2_ok = pd.notna(latest["股息率2"])
    _result("CSI valuation", True, f"rows={summary['rows']}")
    _result("CSI DY1", dy1_ok, f"latest={latest['股息率1']}")
    _result("CSI DY2", dy2_ok, f"latest={latest['股息率2']}")
    print(
        "CSI valuation history: "
        f"{summary['earliest']} -> {summary['latest_date']} ({summary['rows']} observations)"
    )
    print(
        f"Latest valuation: {summary['latest_date']} | DY1={latest['股息率1']} "
        f"| DY2={latest['股息率2']} | PE1={latest['市盈率1']} | PE2={latest['市盈率2']}"
    )
    return dy1_ok and dy2_ok


def _check_cn10y(timeout: int) -> bool:
    start = date.today() - timedelta(days=30)
    end = date.today()
    ok, summary = _isolated_call(
        "bond", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), timeout=timeout
    )
    if not ok:
        return _result("China 10Y", False, str(summary))
    _result("China 10Y", True, f"latest={summary['yield']}")
    print(f"Latest CN10Y date: {summary['date']}")
    return True


def _check_fallback(timeout: int) -> bool:
    ok, summary = _isolated_call("fallback", timeout=timeout)
    if not ok:
        return _result("Bond fallback", False, str(summary))
    return _result("Bond fallback", True, f"latest={summary['latest']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", default="510300", dest="asset_code")
    parser.add_argument("--benchmark", default="000922", dest="benchmark_code")
    parser.add_argument("--test-fallback", action="store_true")
    parser.add_argument("--test-proxy-interfaces", action="store_true")
    parser.add_argument("--proxy-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    print("=== AKShare Data Source Verification ===")
    print(f"AKShare: {version('akshare')}")
    if args.proxy_only:
        proxy_ok = _check_proxy_interfaces(args.asset_code, args.timeout)
        print(f"\nRESULT: {'PASS' if proxy_ok else 'FAIL'}")
        return 0 if proxy_ok else 1

    history_ok, price_ok = _check_asset(args.asset_code, args.timeout)
    proxy_ok = (
        _check_proxy_interfaces(args.asset_code, args.timeout)
        if args.test_proxy_interfaces else True
    )
    csi_ok = _check_csi(args.benchmark_code, args.timeout)
    bond_ok = _check_cn10y(args.timeout)
    fallback_ok = _check_fallback(args.timeout) if args.test_fallback else True
    mandatory_ok = history_ok and price_ok and proxy_ok and csi_ok and bond_ok
    print(f"\nRESULT: {'PASS' if mandatory_ok and fallback_ok else 'FAIL'}")
    return 0 if mandatory_ok and fallback_ok else 1


if __name__ == "__main__":
    sys.exit(main())
