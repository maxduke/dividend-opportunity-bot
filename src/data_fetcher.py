# -*- coding: utf-8 -*-

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
    KEY_FAILURE_COUNT,
    KEY_FAILURE_SENT,
    KEY_HIST_CACHE,
    KEY_HIST_FAILURE_CACHE,
    KEY_NAME_CACHE,
    PRICE_ADJUSTMENT,
    REQUEST_INTERVAL_SECONDS,
    RSI_PERIOD,
    STOCK_PREFIXES,
    TECHNICAL_HISTORY_DAYS,
)
from .market import is_trading_day
from .utils import get_sina_symbol, normalize_hist_df

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RealtimeQuote:
    """A provider quote together with the observation time, when available."""

    price: float
    timestamp: Optional[datetime] = None


@dataclass
class IndicatorPriceSeries:
    """Price observations used by technical indicators.

    ``closes`` never contains a synthetic non-trading-day observation.  When
    a quote cannot be put on the adjusted-price scale, the latest confirmed
    historical close is retained instead.
    """

    closes: pd.Series
    current_price: Optional[float]
    price_date: Optional[date]
    spot_used: bool
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
    """Build an adjusted close series without inventing a daily bar.

    A quote is injected only when its timestamp identifies today's traded
    session (or, for timestamp-less legacy providers, after 09:30 on a
    trading day) and its raw-to-qfq factor is known and valid.
    """
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    today = current.date()

    if hist_df is None or hist_df.empty or "收盘" not in hist_df.columns:
        return IndicatorPriceSeries(pd.Series(dtype=float), None, None, False, "Adjusted history unavailable")

    closes = pd.to_numeric(hist_df["收盘"], errors="coerce").dropna().copy()
    if closes.empty:
        return IndicatorPriceSeries(pd.Series(dtype=float), None, None, False, "Adjusted history unavailable")
    try:
        closes.index = pd.to_datetime(closes.index)
        closes = closes.sort_index()
    except (TypeError, ValueError):
        return IndicatorPriceSeries(pd.Series(dtype=float), None, None, False, "Adjusted history unavailable")

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
        and trading_today
    )

    basis = hist_df.attrs.get("price_basis")
    factor = hist_df.attrs.get("adjust_factor")
    try:
        factor_value = float(factor)
    except (TypeError, ValueError):
        factor_value = None
    factor_valid = factor_value is not None and math.isfinite(factor_value) and factor_value > 0
    adjusted_history = basis == "qfq"
    if not adjusted_history:
        note = (
            "Adjusted ETF history unavailable; technical price basis is unavailable"
            if basis == "unadjusted_fallback"
            else "Adjusted history unavailable; technical price basis is unavailable"
        )
        return IndicatorPriceSeries(closes, latest_price, latest_date, False, note)

    if quote_obj is not None and quote_is_today:
        if factor_valid:
            adjusted_price = quote_obj.price * factor_value
            if math.isfinite(adjusted_price) and adjusted_price > 0:
                if latest_date == today:
                    closes.iloc[-1] = adjusted_price
                else:
                    closes.loc[pd.Timestamp(today)] = adjusted_price
                    closes = closes.sort_index()
                return IndicatorPriceSeries(closes, adjusted_price, today, True, None)
        return IndicatorPriceSeries(
            closes,
            latest_price,
            latest_date,
            False,
            "Realtime adjustment factor unavailable; using the latest confirmed adjusted close",
        )

    if quote_obj is not None and quote_obj.timestamp is None and trading_today and current.time() >= time(9, 30):
        if factor_valid:
            adjusted_price = quote_obj.price * factor_value
            if math.isfinite(adjusted_price) and adjusted_price > 0:
                if latest_date == today:
                    closes.iloc[-1] = adjusted_price
                else:
                    closes.loc[pd.Timestamp(today)] = adjusted_price
                    closes = closes.sort_index()
                return IndicatorPriceSeries(closes, adjusted_price, today, True, None)
        return IndicatorPriceSeries(
            closes,
            latest_price,
            latest_date,
            False,
            "Realtime adjustment factor unavailable; using the latest confirmed adjusted close",
        )

    if latest_date == today:
        return IndicatorPriceSeries(closes, latest_price, latest_date, False, None)

    note = None
    if quote_obj is not None and quote_time is not None and quote_time.date() != today:
        note = "Realtime quote belongs to a previous session; using the latest confirmed adjusted close"
    elif quote_obj is not None and not trading_today:
        note = "Today is not a trading session; using the latest confirmed adjusted close"
    elif quote_obj is not None and current.time() < time(9, 30):
        note = "Market has not opened; using the latest confirmed adjusted close"
    return IndicatorPriceSeries(closes, latest_price, latest_date, False, note)


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
        return await asyncio.wait_for(
            asyncio.to_thread(function, *args, **kwargs),
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


def _cached_history_is_usable(frame, days: int) -> bool:
    return (
        frame is not None
        and frame.attrs.get("technical_history_days", 0) >= days
        and history_is_sufficient(frame, days)
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
        name = f"Asset_{asset_code}"

    name_cache[asset_code] = name
    logger.debug(f"已将新资产名称存入缓存: {asset_code} -> {name}")
    return name


# --- 历史数据获取 ---

async def get_history_data(asset_code: str, days: int) -> Union[pd.DataFrame, None]:
    """获取单个资产的历史日线数据，并在需要时计算复权因子。"""
    try:
        today = datetime.now()
        start_date = (today - timedelta(days=days)).strftime('%Y%m%d')
        end_date = today.strftime('%Y%m%d')
        adjust = PRICE_ADJUSTMENT

        async def fetch_hist_em():
            try:
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
            except Exception as e:
                logger.warning(f"东方财富接口获取历史数据失败({asset_code}): {e}")
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
            df.attrs["adjust_factor"] = None
            df.attrs["price_basis"] = "unadjusted_fallback"
        else:
            df.attrs['adjust_factor'] = await _get_adjust_factor(asset_code, df)
            df.attrs["price_basis"] = "qfq"
        return df
    except Exception as e:
        logger.error(f"获取 {asset_code} 历史数据失败: {e}")
        return None


async def get_history_data_cached(
    context: ContextTypes.DEFAULT_TYPE,
    asset_code: str,
    days: int,
    now: datetime = None,
) -> Union[pd.DataFrame, None]:
    """Share one daily history attempt across jobs and commands."""
    current = now or datetime.now(SHANGHAI_TZ)
    cache = ensure_daily_history_cache(context, current)
    cached = cache.get(asset_code)
    if _cached_history_is_usable(cached, days):
        return cached
    if history_failure_is_fresh(context, asset_code, current):
        return cached

    fetched = await get_history_data(asset_code, days)
    failures = context.bot_data.setdefault(KEY_HIST_FAILURE_CACHE, {})
    if fetched is None or fetched.empty:
        failures[asset_code] = current
        return cached

    cache[asset_code] = fetched
    if history_is_sufficient(fetched, days):
        failures.pop(asset_code, None)
    else:
        # Keep the usable partial history for RSI, but do not pay again every
        # monitoring tick while MA200/52W data is still unavailable.
        failures[asset_code] = current
    return fetched


async def _get_adjust_factor(asset_code: str, hist_df: pd.DataFrame) -> Optional[float]:
    """
    计算复权因子（复权收盘 / 未复权收盘），用于将实时价格转换到复权尺度。
    若无法可靠计算，则返回 ``None``，避免把原始价格混入复权序列。
    """
    try:
        if hist_df is None or hist_df.empty or "收盘" not in hist_df.columns:
            return None
        base_date = pd.Timestamp(hist_df.index[-1])
        today_date = datetime.now(SHANGHAI_TZ).date()
        if base_date.date() >= today_date and len(hist_df.index) > 1:
            base_date = pd.Timestamp(hist_df.index[-2])
        raw_start = (base_date - timedelta(days=30)).strftime('%Y%m%d')
        raw_end = (base_date + timedelta(days=1)).strftime('%Y%m%d')

        async def fetch_raw_hist_em():
            try:
                if asset_code.startswith(STOCK_PREFIXES):
                    return await _call_akshare(
                        ak.stock_zh_a_hist,
                        symbol=asset_code,
                        period="daily",
                        start_date=raw_start,
                        end_date=raw_end,
                        adjust="",
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
                        start_date=raw_start,
                        end_date=raw_end,
                        adjust="",
                        timeout_seconds=(
                            AKSHARE_PROXY_CALL_TIMEOUT_SECONDS
                            if ENABLE_AKSHARE_PROXY_PATCH else None
                        ),
                    )
            except Exception as e:
                logger.warning(f"东方财富接口获取未复权数据失败({asset_code}): {e}")
            return None

        async def fetch_raw_hist_sina():
            try:
                if asset_code.startswith(STOCK_PREFIXES):
                    sina_symbol = get_sina_symbol(asset_code)
                    return await _call_akshare(
                        ak.stock_zh_a_daily,
                        symbol=sina_symbol,
                        start_date=raw_start,
                        end_date=raw_end,
                        adjust="",
                    )
                if asset_code.startswith(ETF_PREFIXES):
                    sina_symbol = get_sina_symbol(asset_code)
                    return await _call_akshare(
                        ak.fund_etf_hist_sina,
                        symbol=sina_symbol,
                    )
            except Exception as e:
                logger.warning(f"新浪接口获取未复权数据失败({asset_code}): {e}")
            return None

        raw_df = None
        sina_attempted = False

        prefer_sina_for_etf_raw = asset_code.startswith(ETF_PREFIXES)
        if ENABLE_AKSHARE_PROXY_PATCH or prefer_sina_for_etf_raw:
            sina_attempted = True
            raw_df = await _run_with_retries(fetch_raw_hist_sina, f"获取未复权数据-新浪({asset_code})")

        if raw_df is None or raw_df.empty:
            raw_df = await _run_with_retries(
                fetch_raw_hist_em,
                f"获取未复权数据({asset_code})",
                attempts=_em_retry_attempts(),
            )

        if (raw_df is None or raw_df.empty) and not sina_attempted:
            logger.info(f"尝试使用新浪接口获取未复权数据({asset_code})。")
            raw_df = await _run_with_retries(fetch_raw_hist_sina, f"获取未复权数据-新浪({asset_code})")
        if raw_df is None or raw_df.empty:
            return None
        raw_df = normalize_hist_df(raw_df)
        if raw_df is None or raw_df.empty or "日期" not in raw_df.columns:
            return None
        raw_df.set_index('日期', inplace=True)

        base_ts = pd.Timestamp(base_date)
        raw_idx = pd.to_datetime(raw_df.index)
        adj_idx = pd.to_datetime(hist_df.index)
        common_dates = raw_idx.intersection(adj_idx)
        candidate_dates = common_dates[common_dates <= base_ts]
        if candidate_dates.empty:
            return None
        aligned_date = candidate_dates.max()

        raw_close = raw_df.loc[aligned_date, '收盘']
        adjusted_close = hist_df.loc[aligned_date, '收盘']
        if isinstance(raw_close, pd.Series):
            raw_close = raw_close.iloc[-1]
        if isinstance(adjusted_close, pd.Series):
            adjusted_close = adjusted_close.iloc[-1]
        if pd.isna(raw_close) or pd.isna(adjusted_close):
            return None
        raw_value = float(raw_close)
        adjusted_value = float(adjusted_close)
        if not math.isfinite(raw_value) or not math.isfinite(adjusted_value):
            return None
        if raw_value <= 0 or adjusted_value <= 0:
            return None
        factor = adjusted_value / raw_value
        return factor if math.isfinite(factor) and factor > 0 else None
    except Exception as e:
        logger.warning(f"计算复权因子失败({asset_code}): {e}")
        return None


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


async def _fetch_single_realtime_price(code: str) -> Union[float, None]:
    """Compatibility wrapper; the provider is called exactly once per attempt."""
    quote = await _fetch_single_realtime_quote(code)
    return quote.price if quote is not None else None


async def _fetch_all_realtime_quotes(
    context: ContextTypes.DEFAULT_TYPE,
    codes: List[str],
) -> Tuple[Dict[str, RealtimeQuote], bool]:
    """Fetch timestamp-preserving quotes while retaining existing failure state."""
    quote_dict: Dict[str, RealtimeQuote] = {}
    for code in codes:
        await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
        quote = await _fetch_single_realtime_quote(code)
        if quote is not None:
            quote_dict[code] = quote

    if not quote_dict and codes:
        logger.warning("本次未获取到任何有效价格。")
        context.bot_data[KEY_FAILURE_COUNT] = context.bot_data.get(KEY_FAILURE_COUNT, 0) + 1
        count = context.bot_data[KEY_FAILURE_COUNT]
        if count >= FETCH_FAILURE_THRESHOLD and not context.bot_data.get(KEY_FAILURE_SENT) and ADMIN_USER_ID:
            admin_message = f"🚨 **机器人警报** 🚨\n\n连续获取数据失败已达 **{count}** 次。\n请检查新浪接口连通性。"
            try:
                await context.bot.send_message(chat_id=ADMIN_USER_ID, text=admin_message, parse_mode=ParseMode.MARKDOWN)
                logger.warning("已向管理员发送数据获取失败的警报通知。")
                context.bot_data[KEY_FAILURE_SENT] = True
            except Exception as e:
                logger.error(f"向管理员发送数据获取失败告警时出错: {e}")
        return {}, False

    if context.bot_data.get(KEY_FAILURE_COUNT, 0) > 0:
        logger.info("数据获取成功，重置失败计数器。")
    context.bot_data[KEY_FAILURE_COUNT] = 0
    context.bot_data[KEY_FAILURE_SENT] = False
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
