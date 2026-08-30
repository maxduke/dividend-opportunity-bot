# -*- coding: utf-8 -*-

import asyncio
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .config import (
    ADMIN_USER_ID,
    AKSHARE_CALL_TIMEOUT_SECONDS,
    AKSHARE_PROXY_CALL_TIMEOUT_SECONDS,
    ENABLE_AKSHARE_PROXY_PATCH,
    ETF_PREFIXES,
    FETCH_FAILURE_THRESHOLD,
    FETCH_RETRY_ATTEMPTS,
    FETCH_RETRY_DELAY_SECONDS,
    HISTORY_FAILURE_COOLDOWN_MINUTES,
    KEY_CACHE_DATE,
    KEY_HIST_CACHE,
    KEY_HIST_FAILURE_CACHE,
    KEY_NAME_CACHE,
    KEY_QUOTE_FAILURE_COUNTS,
    KEY_QUOTE_FAILURE_NOTIFIED,
    PRICE_ADJUSTMENT,
    REQUEST_INTERVAL_SECONDS,
    RSI_PERIOD,
    STOCK_PREFIXES,
    TECHNICAL_HISTORY_DAYS,
)
from .market import is_trading_day
from .proxy_health import (
    POSITIVE,
    check_proxy_balance_async,
    notify_proxy_health,
    proxy_patch_active,
)
from .utils import get_sina_symbol, normalize_hist_df

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# ponytail: bound abandoned provider threads; use subprocess isolation if calls
# can hang permanently in production.
_AKSHARE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="akshare")


@dataclass(frozen=True)
class RealtimeQuote:
    """A provider quote together with the observation time, when available."""

    price: float
    timestamp: Optional[datetime] = None


@dataclass
class IndicatorPriceSeries:
    """Price observations used by technical indicators.

    ``closes`` never contains a synthetic non-trading-day observation.  When
    a quote cannot be safely merged, the latest confirmed historical close is
    retained instead.
    """

    closes: pd.Series
    current_price: Optional[float]
    price_date: Optional[date]
    spot_used: bool
    degraded: bool = False
    note: Optional[str] = None


def _to_shanghai_datetime(value) -> Optional[datetime]:
    """Parse a provider timestamp without allowing timezone exceptions out."""
    if value is None or (not isinstance(value, (datetime, pd.Timestamp)) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None
    try:
        if isinstance(value, pd.Timestamp):
            parsed = value.to_pydatetime()
        elif isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            # A bare minute ``time`` has no session date and must not be
            # silently assigned today's date by pandas.
            if len(text) <= 8 and text.count(":") >= 1:
                return None
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = pd.to_datetime(value, errors="coerce").to_pydatetime()
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
    if parsed is None or pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def _extract_realtime_timestamp(frame: pd.DataFrame, row) -> Optional[datetime]:
    """Extract the last-minute timestamp across AKShare's known schemas."""
    candidates = (
        "day", "datetime", "date", "time", "date_time",
        "日期时间", "日期", "时间", "日期时间戳",
    )
    columns = {str(column).lower(): column for column in frame.columns}
    for name in candidates:
        column = columns.get(name.lower())
        if column is None:
            continue
        timestamp = _to_shanghai_datetime(row[column])
        if timestamp is not None:
            return timestamp
    try:
        return _to_shanghai_datetime(frame.index[-1])
    except (IndexError, KeyError):
        return None


def _quote_from_value(quote) -> Optional[RealtimeQuote]:
    """Accept a quote object and old float callers at one compatibility edge."""
    if isinstance(quote, RealtimeQuote):
        try:
            price = float(quote.price)
        except (TypeError, ValueError):
            return None
        return quote if math.isfinite(price) and price > 0 else None
    try:
        price = float(quote)
    except (TypeError, ValueError):
        return None
    return RealtimeQuote(price, None) if math.isfinite(price) and price > 0 else None


def _is_trading_day(now: datetime) -> bool:
    """Use the existing XSHG calendar, with a safe weekday fallback on failure."""
    try:
        return bool(is_trading_day(now))
    except Exception as exc:
        # Calendar outages must not manufacture a bar on an obvious weekend.
        logger.warning("交易日历不可用，使用工作日保守回退: %s", exc)
        return False


def build_indicator_close_series(
    hist_df: Optional[pd.DataFrame],
    quote: Optional[RealtimeQuote],
    now: Optional[datetime] = None,
) -> IndicatorPriceSeries:
    """Build a qfq close series without inventing a daily bar."""
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    today = current.date()

    if hist_df is None or hist_df.empty or "收盘" not in hist_df.columns:
        return IndicatorPriceSeries(
            pd.Series(dtype=float), None, None, False, True,
            "复权历史数据不可用",
        )

    closes = pd.to_numeric(hist_df["收盘"], errors="coerce").dropna().copy()
    if closes.empty:
        return IndicatorPriceSeries(
            pd.Series(dtype=float), None, None, False, True,
            "复权历史数据不可用",
        )
    try:
        closes.index = pd.to_datetime(closes.index)
        closes = closes.sort_index()
    except (TypeError, ValueError):
        return IndicatorPriceSeries(
            pd.Series(dtype=float), None, None, False, True,
            "复权历史数据不可用",
        )

    latest_index = closes.index[-1]
    if getattr(latest_index, "tzinfo", None) is not None:
        latest_date = latest_index.tz_convert(SHANGHAI_TZ).date()
    else:
        latest_date = latest_index.date()
    latest_price = float(closes.iloc[-1])
    quote_obj = _quote_from_value(quote)
    trading_today = _is_trading_day(current)
    quote_time = _to_shanghai_datetime(quote_obj.timestamp) if quote_obj else None
    quote_is_today = bool(
        quote_time is not None
        and quote_time.date() == today
        and quote_time <= current
        and quote_time.time() >= time(9, 30)
        and trading_today
    )

    basis = hist_df.attrs.get("price_basis")
    basis_asof = hist_df.attrs.get("price_basis_asof")
    try:
        basis_asof = date.fromisoformat(str(basis_asof)[:10])
    except (TypeError, ValueError):
        basis_asof = None
    current_qfq = basis == "qfq" and (basis_asof == today or latest_date == today)
    if not current_qfq:
        if basis != "qfq":
            note = (
                "ETF 复权历史数据不可用，技术价格基准不可用"
                if basis == "unadjusted_fallback"
                else "复权历史数据不可用，技术价格基准不可用"
            )
        else:
            note = (
                "前复权历史基准尚未确认适用于当前上海日期，"
                "使用最近确认的 qfq 收盘价"
            )
        return IndicatorPriceSeries(
            closes,
            latest_price,
            latest_date,
            False,
            True,
            note,
        )

    if quote_obj is not None and quote_is_today:
        if latest_date == today:
            closes.iloc[-1] = quote_obj.price
        else:
            closes.loc[pd.Timestamp(today)] = quote_obj.price
            closes = closes.sort_index()
        return IndicatorPriceSeries(closes, quote_obj.price, today, True, False, None)

    note = None
    degraded = False
    if quote_obj is not None and quote_time is not None and quote_time.date() != today:
        note = "实时行情属于上一个交易日，使用最近确认的复权收盘价"
        degraded = True
    elif quote_obj is not None and not trading_today:
        note = "今天不是交易日，使用最近确认的复权收盘价"
        degraded = True
    elif quote_obj is None:
        note = "实时行情不可用，使用最近确认的 qfq 收盘价"
        degraded = True
    elif quote_time is None:
        note = "实时行情时间不可用，使用最近确认的 qfq 收盘价"
        degraded = True
    elif quote_time > current:
        note = "实时行情时间在未来，使用最近确认的 qfq 收盘价"
        degraded = True
    elif quote_time.time() < time(9, 30):
        note = "实时行情时间早于开盘时间，使用最近确认的 qfq 收盘价"
        degraded = True
    return IndicatorPriceSeries(closes, latest_price, latest_date, False, degraded, note)


# --- 重试逻辑（改进5: 指数退避） ---

async def _run_with_retries(operation, description: str, attempts: int = None):
    attempts = FETCH_RETRY_ATTEMPTS if attempts is None else attempts
    for attempt in range(1, attempts + 1):
        result = await operation()
        if result is not None:
            return result
        if attempt < attempts:
            delay = FETCH_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                f"{description} 失败，{delay}秒后重试 "
                f"({attempt}/{attempts})。"
            )
            await asyncio.sleep(delay)
    return None


async def _call_akshare(function, *args, timeout_seconds=None, **kwargs):
    """Run a blocking provider call without blocking the bot event loop.

    ``wait_for`` limits how long the application waits; it cannot forcibly
    terminate an already-running Python worker thread.
    """
    timeout = AKSHARE_CALL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(_AKSHARE_EXECUTOR, partial(function, *args, **kwargs)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "AKShare调用超时(%ss): %s",
            timeout,
            getattr(function, "__name__", repr(function)),
        )
        return None


def _em_retry_attempts():
    # The proxy itself already retries target requests; an application retry
    # would multiply billed attempts.
    return 1 if ENABLE_AKSHARE_PROXY_PATCH else None


def _valid_close_count(frame) -> int:
    if frame is None or getattr(frame, "empty", True):
        return 0
    column = "收盘" if "收盘" in frame.columns else "close" if "close" in frame.columns else None
    if column is None:
        return 0
    return int(pd.to_numeric(frame[column], errors="coerce").notna().sum())


def history_is_sufficient(frame, days: int) -> bool:
    minimum = 252 if days >= TECHNICAL_HISTORY_DAYS else RSI_PERIOD + 1
    return _valid_close_count(frame) >= minimum


def runtime_history_is_usable(frame, days: int, now=None) -> bool:
    """Return whether a frame is current qfq history suitable for runtime scoring."""
    if frame is None or getattr(frame, "empty", True):
        return False
    current = now or datetime.now(SHANGHAI_TZ)
    if isinstance(current, datetime):
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI_TZ)
        current_date = current.astimezone(SHANGHAI_TZ).date()
    else:
        current_date = current
    try:
        basis_asof = date.fromisoformat(str(frame.attrs.get("price_basis_asof"))[:10])
    except (TypeError, ValueError):
        return False
    return (
        frame.attrs.get("technical_history_days", 0) >= days
        and history_is_sufficient(frame, days)
        and frame.attrs.get("price_basis") == PRICE_ADJUSTMENT == "qfq"
        and basis_asof == current_date
    )


def history_failure_is_fresh(context: ContextTypes.DEFAULT_TYPE, asset_code: str, now=None) -> bool:
    if HISTORY_FAILURE_COOLDOWN_MINUTES <= 0:
        return False
    failed_at = context.bot_data.get(KEY_HIST_FAILURE_CACHE, {}).get(str(asset_code))
    if not failed_at:
        return False
    if not isinstance(failed_at, datetime):
        return False
    current = now or datetime.now(SHANGHAI_TZ)
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    return (current - failed_at).total_seconds() < HISTORY_FAILURE_COOLDOWN_MINUTES * 60


# --- 缓存 ---

def ensure_daily_history_cache(context: ContextTypes.DEFAULT_TYPE, now: datetime) -> Dict[str, pd.DataFrame]:
    bot_data = context.bot_data
    today_str = now.strftime('%Y-%m-%d')
    if bot_data.get(KEY_CACHE_DATE) != today_str:
        logger.info(f"日期变更或首次运行，清空并重建 {today_str} 的历史数据缓存。")
        bot_data[KEY_HIST_CACHE] = {}
        bot_data[KEY_HIST_FAILURE_CACHE] = {}
        bot_data[KEY_CACHE_DATE] = today_str
    return bot_data.get(KEY_HIST_CACHE, {})


# --- 资产名称缓存 ---

async def get_asset_name_with_cache(asset_code: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    name_cache = context.bot_data.setdefault(KEY_NAME_CACHE, {})

    if asset_code in name_cache:
        logger.debug(f"从缓存命中资产名称: {asset_code} -> {name_cache[asset_code]}")
        return name_cache[asset_code]

    logger.info(f"缓存未命中，尝试获取资产名称: {asset_code}")
    await asyncio.sleep(REQUEST_INTERVAL_SECONDS)

    async def fetch_name():
        if asset_code.startswith(STOCK_PREFIXES):
            if ENABLE_AKSHARE_PROXY_PATCH:
                if not proxy_patch_active():
                    logger.info("proxy 未激活，跳过股票名称 EastMoney 请求")
                    return None
                balance_status = await check_proxy_balance_async()
                if balance_status.state != POSITIVE:
                    logger.info(
                        "proxy 不可用，跳过股票名称 EastMoney 请求 balance_status=%s",
                        balance_status.state,
                    )
                    return None
            info_df = await _call_akshare(
                ak.stock_individual_info_em,
                symbol=asset_code,
                timeout_seconds=AKSHARE_PROXY_CALL_TIMEOUT_SECONDS if ENABLE_AKSHARE_PROXY_PATCH else None,
            )
            if info_df is not None and not info_df.empty and 'value' in info_df.columns:
                match = info_df.loc[info_df['item'] == '股票简称', 'value']
                if not match.empty:
                    return match.iloc[0]
        if asset_code.startswith(ETF_PREFIXES):
            if ENABLE_AKSHARE_PROXY_PATCH:
                # akshare-proxy-patch deliberately bypasses .js/.html URLs;
                # fund_name_em would therefore hit EastMoney directly.
                logger.info("proxy 模式跳过 fund_name_em(.js)，使用代码作为 ETF 名称回退")
                return None
            name_df = await _call_akshare(
                ak.fund_name_em,
                timeout_seconds=AKSHARE_PROXY_CALL_TIMEOUT_SECONDS if ENABLE_AKSHARE_PROXY_PATCH else None,
            )
            if name_df is not None and not name_df.empty:
                match = name_df.loc[name_df['基金代码'] == asset_code, '基金简称']
                if not match.empty:
                    return match.iloc[0]
        return None

    name = await _run_with_retries(
        fetch_name,
        f"获取资产名称({asset_code})",
        attempts=_em_retry_attempts(),
    )
    if not name:
        name = f"资产_{asset_code}"

    name_cache[asset_code] = name
    logger.debug(f"已将新资产名称存入缓存: {asset_code} -> {name}")
    return name


# --- 历史数据获取 ---

async def get_history_data(
    asset_code: str,
    days: int,
    price_adjust: str = PRICE_ADJUSTMENT,
) -> Union[pd.DataFrame, None]:
    """获取单个资产的历史日线数据。"""
    if price_adjust not in {"qfq", "hfq"}:
        raise ValueError("price_adjust must be 'qfq' or 'hfq'")
    try:
        today = datetime.now(SHANGHAI_TZ)
        start_date = (today - timedelta(days=days)).strftime('%Y%m%d')
        end_date = today.strftime('%Y%m%d')
        adjust = price_adjust

        async def fetch_hist_em():
            try:
                if ENABLE_AKSHARE_PROXY_PATCH:
                    balance_status = await check_proxy_balance_async()
                    if not proxy_patch_active() or balance_status.state != POSITIVE:
                        logger.warning(
                            "[AKSHARE] paid history skipped balance_status=%s patch_active=%s",
                            balance_status.state,
                            proxy_patch_active(),
                        )
                        return None
                if asset_code.startswith(STOCK_PREFIXES):
                    return await _call_akshare(
                        ak.stock_zh_a_hist,
                        symbol=asset_code,
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                        timeout_seconds=(
                            AKSHARE_PROXY_CALL_TIMEOUT_SECONDS
                            if ENABLE_AKSHARE_PROXY_PATCH else None
                        ),
                    )
                if asset_code.startswith(ETF_PREFIXES):
                    return await _call_akshare(
                        ak.fund_etf_hist_em,
                        symbol=asset_code,
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                        timeout_seconds=(
                            AKSHARE_PROXY_CALL_TIMEOUT_SECONDS
                            if ENABLE_AKSHARE_PROXY_PATCH else None
                        ),
                    )
            except Exception as exc:
                if ENABLE_AKSHARE_PROXY_PATCH:
                    logger.warning(
                        "东方财富接口获取历史数据失败(%s) error_type=%s",
                        asset_code,
                        type(exc).__name__,
                    )
                else:
                    logger.warning("东方财富接口获取历史数据失败(%s): %s", asset_code, exc)
            return None

        async def fetch_hist_sina():
            try:
                sina_symbol = get_sina_symbol(asset_code)
                if asset_code.startswith(STOCK_PREFIXES):
                    return await _call_akshare(
                        ak.stock_zh_a_daily,
                        symbol=sina_symbol,
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                    )
                if asset_code.startswith(ETF_PREFIXES):
                    return await _call_akshare(
                        ak.fund_etf_hist_sina,
                        symbol=sina_symbol,
                    )
            except Exception as e:
                logger.warning(f"新浪接口获取历史数据失败({asset_code}): {e}")
            return None

        df = None
        source = "sina"
        proxy_can_use_free_history = not asset_code.startswith(ETF_PREFIXES)
        if ENABLE_AKSHARE_PROXY_PATCH and proxy_can_use_free_history:
            # Sina is not in the proxy hook list. Use it first and pay for
            # EastMoney only when Sina is unavailable or too short. Adjusted
            # ETF history is the exception because Sina only supplies raw data.
            df = await _run_with_retries(fetch_hist_sina, f"获取历史数据-新浪({asset_code})")
            if not history_is_sufficient(df, days):
                logger.info(f"新浪历史数据不足，才尝试东方财富({asset_code})。")
                em_df = await _run_with_retries(
                    fetch_hist_em,
                    f"获取历史数据({asset_code})",
                    attempts=_em_retry_attempts(),
                )
                if em_df is not None and (
                    df is None or _valid_close_count(em_df) >= _valid_close_count(df)
                ):
                    df = em_df
                    source = "em"
        else:
            df = await _run_with_retries(
                fetch_hist_em,
                f"获取历史数据({asset_code})",
                attempts=_em_retry_attempts(),
            )
            source = "em"
        # Bug3: 统一使用 is None or empty 判断
        if not history_is_sufficient(df, days):
            logger.info(f"尝试使用新浪接口获取历史数据({asset_code})。")
            sina_df = await _run_with_retries(fetch_hist_sina, f"获取历史数据-新浪({asset_code})")
            if sina_df is not None and (
                df is None or _valid_close_count(sina_df) >= _valid_close_count(df)
            ):
                df = sina_df
                source = "sina"
        if df is None or df.empty:
            return None
        df = normalize_hist_df(df)
        if df is None or df.empty or "日期" not in df.columns:
            return None
        df.set_index("日期", inplace=True)
        df.attrs["technical_history_days"] = days
        if source == "sina" and asset_code.startswith(ETF_PREFIXES):
            logger.info(f"ETF({asset_code}) 使用新浪历史数据，仅能提供不复权数据。")
            df.attrs["price_basis"] = "unadjusted_fallback"
            df.attrs["price_basis_asof"] = datetime.now(SHANGHAI_TZ).date().isoformat()
        else:
            df.attrs["price_basis"] = price_adjust
            df.attrs["price_basis_asof"] = datetime.now(SHANGHAI_TZ).date().isoformat()
        return df
    except Exception as exc:
        if ENABLE_AKSHARE_PROXY_PATCH:
            logger.error(
                "获取 %s 历史数据失败 error_type=%s",
                asset_code,
                type(exc).__name__,
            )
        else:
            logger.error("获取 %s 历史数据失败: %s", asset_code, exc)
        return None


async def get_history_data_cached(
    context: ContextTypes.DEFAULT_TYPE,
    asset_code: str,
    days: int,
    now: datetime = None,
) -> Union[pd.DataFrame, None]:
    """Share one daily history attempt across jobs and commands."""
    current = now or datetime.now(SHANGHAI_TZ)
    locks = context.bot_data.setdefault("hist_data_locks", {})
    lock = locks.setdefault(asset_code, asyncio.Lock())
    async with lock:
        # Re-check after waiting: another caller may have populated the cache.
        cache = ensure_daily_history_cache(context, current)
        cached = cache.get(asset_code)
        if runtime_history_is_usable(cached, days, current):
            return cached
        if history_failure_is_fresh(context, asset_code, current):
            return cached

        fetched = await get_history_data(asset_code, days)
        bot = getattr(context, "bot", None)
        if bot is not None:
            await notify_proxy_health(bot)
        failures = context.bot_data.setdefault(KEY_HIST_FAILURE_CACHE, {})
        if fetched is None or fetched.empty:
            failures[asset_code] = current
            return cached

        cache[asset_code] = fetched
        if runtime_history_is_usable(fetched, days, current):
            failures.pop(asset_code, None)
        else:
            # Retain degraded history for display, but keep technical scoring off
            # and avoid another paid attempt until the cooldown expires.
            failures[asset_code] = current
        return fetched


# --- 实时价格获取 ---

async def _fetch_single_realtime_quote(code: str) -> Optional[RealtimeQuote]:
    """通过新浪分时接口获取最新报价及其观测时间。"""
    sina_symbol = get_sina_symbol(code)

    async def fetch_quote():
        try:
            df = await _call_akshare(ak.stock_zh_a_minute, symbol=sina_symbol, period='1')
            if df is not None and not df.empty:
                row = df.iloc[-1]
                close_column = next(
                    (column for column in ("close", "收盘") if column in df.columns),
                    None,
                )
                if close_column is None:
                    return None
                price = float(row[close_column])
                if not math.isfinite(price) or price <= 0:
                    return None
                return RealtimeQuote(price, _extract_realtime_timestamp(df, row))
        except asyncio.TimeoutError:
            logger.warning(f"获取 {code} 实时价格超时")
        except Exception as e:
            logger.warning(f"获取 {code} 实时价格失败: {e}")
        return None

    return await _run_with_retries(fetch_quote, f"获取实时价格({code})")


async def _fetch_all_realtime_quotes(
    context: ContextTypes.DEFAULT_TYPE,
    codes: List[str],
) -> Tuple[Dict[str, RealtimeQuote], bool]:
    """Fetch timestamp-preserving quotes while retaining existing failure state."""
    # ponytail: one bot-wide lock; use per-asset locks only if quote throughput matters.
    fetch_lock = context.bot_data.setdefault("quote_fetch_lock", asyncio.Lock())
    async with fetch_lock:
        quote_dict: Dict[str, RealtimeQuote] = {}
        for code in codes:
            await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
            quote = await _fetch_single_realtime_quote(code)
            if quote is not None:
                quote_dict[code] = quote

        if not quote_dict and codes:
            logger.warning("本次未获取到任何有效价格。")
        failure_counts = context.bot_data.setdefault(KEY_QUOTE_FAILURE_COUNTS, {})
        failure_notified = context.bot_data.setdefault(KEY_QUOTE_FAILURE_NOTIFIED, {})
        for code in codes:
            if code in quote_dict:
                if code in failure_counts:
                    logger.info("数据获取成功，重置 %s 失败计数器。", code)
                    failure_counts.pop(code, None)
                failure_notified.pop(code, None)
            else:
                failure_counts[code] = failure_counts.get(code, 0) + 1

        if ADMIN_USER_ID:
            pending = {
                code: failure_counts[code]
                for code in codes
                if failure_counts.get(code, 0) >= FETCH_FAILURE_THRESHOLD
                and not failure_notified.get(code, False)
            }
            if pending:
                try:
                    pending_items = list(pending.items())
                    # ponytail: 50 six-digit codes stay well below Telegram's limit.
                    for offset in range(0, len(pending_items), 50):
                        batch = dict(pending_items[offset:offset + 50])
                        details = "\n".join(
                            f"- `{code}`：连续失败 {count} 次"
                            for code, count in batch.items()
                        )
                        admin_message = (
                            "🚨 **机器人警报** 🚨\n\n"
                            "以下资产连续获取报价失败已达到阈值：\n"
                            f"{details}\n\n请检查行情接口连通性。"
                        )
                        await context.bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text=admin_message,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        for code, count in batch.items():
                            if failure_counts.get(code, 0) >= count:
                                failure_notified[code] = True
                    logger.warning(
                        "已向管理员发送数据获取失败的警报通知：%s",
                        ", ".join(pending),
                    )
                except Exception as e:
                    logger.error(f"向管理员发送数据获取失败告警时出错: {e}")

        if not quote_dict and codes:
            return {}, False
        return quote_dict, True


# --- RSI 计算 ---

def calculate_rsi_wilder(prices: pd.Series, period: int = 6) -> Union[float, None]:
    """Calculate RSI with recursive Wilder (Chinese ``SMA(X,N,1)``) smoothing."""
    try:
        if period <= 0 or prices is None:
            return None
        values = pd.to_numeric(pd.Series(prices), errors="coerce").dropna()
        if len(values) < period + 1:
            return None
        delta = values.diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
        if pd.isna(avg_gain) or pd.isna(avg_loss):
            return None
        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        if avg_gain == 0:
            return 0.0
        result = 100 - (100 / (1 + avg_gain / avg_loss))
        return round(float(result), 2) if math.isfinite(result) else None
    except Exception as e:
        logger.error(f"RSI计算出错: {e}")
        return None


def calculate_rsi(prices: pd.Series) -> Union[float, None]:
    return calculate_rsi_wilder(prices, period=RSI_PERIOD)
