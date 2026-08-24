"""CSI valuation and China 10Y data access, persistence, and cache."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from .config import (
    BOND_CACHE_HOURS,
    REQUEST_INTERVAL_SECONDS,
    VALUATION_CACHE_HOURS,
)
from .data_fetcher import _call_akshare, _run_with_retries
from .database import db_execute, db_executemany

logger = logging.getLogger(__name__)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
VALUATION_FAILURE_CACHE_KEY = "valuation_failure_cache"
BOND_FAILURE_CACHE_KEY = "bond_failure_cache"
VALUATION_CACHE_LOCK_KEY = "valuation_cache_lock"
BOND_CACHE_LOCK_KEY = "bond_cache_lock"


def _now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _as_number(value) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    try:
        text = str(value).strip().replace("%", "")
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def normalize_csi_valuation(frame: pd.DataFrame, benchmark_code: str) -> Optional[pd.DataFrame]:
    if frame is None or frame.empty:
        return None
    required = ("日期", "股息率1", "股息率2")
    if any(column not in frame.columns for column in required):
        logger.warning("中证估值字段不完整: %s", list(frame.columns))
        return None

    result = frame.copy()
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce").dt.date
    result = result.dropna(subset=["日期"])
    if result.empty:
        return None
    for column in ("市盈率1", "市盈率2", "股息率1", "股息率2"):
        if column not in result.columns:
            result[column] = None
        result[column] = result[column].map(_as_number)
    if "benchmark_name" in result.columns:
        result["benchmark_name"] = result["benchmark_name"].where(result["benchmark_name"].notna(), None)
    elif "指数中文简称" in result.columns:
        result["benchmark_name"] = result["指数中文简称"].where(result["指数中文简称"].notna(), None)
    elif "指数名称" in result.columns:
        result["benchmark_name"] = result["指数名称"].where(result["指数名称"].notna(), None)
    else:
        result["benchmark_name"] = None
    result["benchmark_code"] = str(benchmark_code)
    return result.sort_values("日期").drop_duplicates("日期", keep="last").reset_index(drop=True)


async def fetch_csi_valuation(benchmark_code: str) -> Optional[pd.DataFrame]:
    async def operation():
        try:
            frame = await _call_akshare(ak.stock_zh_index_value_csindex, symbol=benchmark_code)
            return normalize_csi_valuation(frame, benchmark_code)
        except Exception as exc:
            logger.warning("[VALUATION] 获取中证估值失败(%s): %s", benchmark_code, exc)
            return None

    return await _run_with_retries(operation, f"获取中证估值({benchmark_code})")


def persist_valuation_rows(
    benchmark_code: str,
    frame: Optional[pd.DataFrame],
    fetched_at: Optional[datetime] = None,
) -> int:
    frame = normalize_csi_valuation(frame, benchmark_code) if frame is not None else None
    if frame is None or frame.empty:
        return 0
    fetched = (fetched_at or _now()).isoformat()
    rows = []
    for _, row in frame.iterrows():
        rows.append(
            (
                str(benchmark_code),
                row["日期"].isoformat(),
                row.get("benchmark_name"),
                row.get("市盈率1"),
                row.get("市盈率2"),
                row.get("股息率1"),
                row.get("股息率2"),
                "csindex",
                fetched,
            )
        )
    db_executemany(
        """
        INSERT OR REPLACE INTO benchmark_valuation_snapshots (
            benchmark_code, valuation_date, benchmark_name, pe1, pe2,
            dividend_yield1, dividend_yield2, source, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def get_latest_valuation(benchmark_code: str):
    return db_execute(
        """
        SELECT * FROM benchmark_valuation_snapshots
        WHERE benchmark_code = ?
        ORDER BY valuation_date DESC
        LIMIT 1
        """,
        (str(benchmark_code),),
        fetchone=True,
    )


def get_valuation_history(benchmark_code: str, start_date: date, end_date: Optional[date] = None):
    end_date = end_date or _now().date()
    return db_execute(
        """
        SELECT * FROM benchmark_valuation_snapshots
        WHERE benchmark_code = ? AND valuation_date >= ? AND valuation_date <= ?
        ORDER BY valuation_date
        """,
        (str(benchmark_code), start_date.isoformat(), end_date.isoformat()),
        fetchall=True,
    ) or []


def get_bond_history(start_date: Optional[date] = None, end_date: Optional[date] = None):
    query = "SELECT * FROM macro_yield_snapshots"
    params = []
    conditions = []
    if start_date:
        conditions.append("yield_date >= ?")
        params.append(start_date.isoformat())
    if end_date:
        conditions.append("yield_date <= ?")
        params.append(end_date.isoformat())
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY yield_date"
    return db_execute(query, tuple(params), fetchall=True) or []


def _cache_is_fresh(entry: Optional[dict], hours: float) -> bool:
    if not entry:
        return False
    fetched_at = entry["fetched_at"]
    if not fetched_at:
        return False
    if isinstance(fetched_at, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=SHANGHAI_TZ)
    return (_now() - fetched_at).total_seconds() < hours * 3600


async def get_cached_valuation(benchmark_code: str, bot_data: dict):
    lock = bot_data.setdefault(VALUATION_CACHE_LOCK_KEY, asyncio.Lock())
    async with lock:
        code = str(benchmark_code)
        failures = bot_data.setdefault(VALUATION_FAILURE_CACHE_KEY, {})
        latest = get_latest_valuation(code)
        if _cache_is_fresh(latest, VALUATION_CACHE_HOURS):
            logger.info("[VALUATION] %s 命中持久化缓存，跳过网络请求", code)
            return latest
        if _cache_is_fresh({"fetched_at": failures.get(code)}, VALUATION_CACHE_HOURS):
            return latest

        frame = await fetch_csi_valuation(code)
        if frame is not None:
            failures.pop(code, None)
            count = persist_valuation_rows(code, frame)
            logger.info("[VALUATION] %s 保存 %s 条中证估值记录", code, count)
        else:
            failures[code] = _now()
            logger.warning("[VALUATION] %s 刷新失败，使用本地已有估值（如有）", code)
        return get_latest_valuation(code)


def normalize_bond_frame(frame: pd.DataFrame) -> Optional[pd.DataFrame]:
    if frame is None or frame.empty:
        return None
    if "10年" not in frame.columns:
        logger.warning("中债收益率字段不完整: %s", list(frame.columns))
        return None
    if "曲线名称" in frame.columns:
        result = frame[frame["曲线名称"] == "中债国债收益率曲线"].copy()
    else:
        # Some AKShare releases fix the curve in the request
        # but omits the curve-name column from the returned table.
        logger.info("[BOND] bond_china_yield 未返回曲线名称列，按接口固定的国债曲线处理")
        result = frame.copy()
    date_column = "日期" if "日期" in result.columns else "date"
    if date_column not in result.columns:
        return None
    result["日期"] = pd.to_datetime(result[date_column], errors="coerce").dt.date
    # Both AKShare bond providers document yield values directly in percent.
    result["cn10y"] = result["10年"].map(_as_number)
    result = result.dropna(subset=["日期", "cn10y"])
    return result[["日期", "cn10y"]].sort_values("日期").drop_duplicates("日期", keep="last") if not result.empty else None


def normalize_sina_bond_frame(frame: pd.DataFrame) -> Optional[pd.DataFrame]:
    if frame is None or frame.empty:
        return None
    date_column = next((column for column in ("日期", "date", "日期时间") if column in frame.columns), None)
    close_column = next((column for column in ("close", "收盘", "最新价") if column in frame.columns), None)
    if not date_column or not close_column:
        logger.warning("新浪国债字段不完整: %s", list(frame.columns))
        return None
    result = pd.DataFrame({"日期": pd.to_datetime(frame[date_column], errors="coerce").dt.date,
                           "cn10y": frame[close_column].map(_as_number)})
    result = result.dropna(subset=["日期", "cn10y"])
    return result.sort_values("日期").drop_duplicates("日期", keep="last") if not result.empty else None


async def _fetch_primary_bond(start_date: date, end_date: date) -> Optional[pd.DataFrame]:
    async def operation():
        try:
            raw = await _call_akshare(
                ak.bond_china_yield,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
            return normalize_bond_frame(raw)
        except Exception as exc:
            logger.warning("[BOND] 中债接口失败(%s至%s): %s", start_date, end_date, exc)
            return None

    return await _run_with_retries(operation, f"获取中债10Y({start_date}至{end_date})")


async def fetch_cn10y() -> tuple[Optional[pd.DataFrame], str]:
    end_date = _now().date()
    primary = await _fetch_primary_bond(end_date - timedelta(days=30), end_date)
    if primary is not None and not primary.empty:
        return primary, "chinabond"

    async def fallback():
        try:
            raw = await _call_akshare(ak.bond_gb_zh_sina, symbol="中国10年期国债")
            return normalize_sina_bond_frame(raw)
        except Exception as exc:
            logger.warning("[BOND] 新浪备用接口失败: %s", exc)
            return None

    fallback_frame = await _run_with_retries(fallback, "获取新浪10Y备用数据")
    if fallback_frame is not None and not fallback_frame.empty:
        fallback_frame = fallback_frame.tail(1).copy()
    return fallback_frame, "sina"


def persist_bond_rows(frame: Optional[pd.DataFrame], source: str, fetched_at: Optional[datetime] = None) -> int:
    if frame is None or frame.empty:
        return 0
    fetched = (fetched_at or _now()).isoformat()
    rows = [
        (row["日期"].isoformat(), float(row["cn10y"]), source, fetched)
        for _, row in frame.iterrows()
    ]
    db_executemany(
        """
        INSERT INTO macro_yield_snapshots (yield_date, cn10y, source, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(yield_date) DO UPDATE SET
            cn10y = excluded.cn10y,
            source = excluded.source,
            fetched_at = excluded.fetched_at
        WHERE macro_yield_snapshots.source <> 'chinabond'
           OR excluded.source = 'chinabond'
        """,
        rows,
    )
    return len(rows)


def latest_bond_on_or_before(target_date: date, max_gap_days: int = 7):
    rows = get_bond_history(end_date=target_date)
    if not rows:
        return None
    candidates = []
    for row in rows:
        try:
            row_date = date.fromisoformat(row["yield_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if row_date <= target_date:
            candidates.append((row_date, row))
    if not candidates:
        return None
    row_date, row = max(candidates, key=lambda item: item[0])
    return row if (target_date - row_date).days <= max_gap_days else None


async def get_cached_cn10y(bot_data: dict):
    lock = bot_data.setdefault(BOND_CACHE_LOCK_KEY, asyncio.Lock())
    async with lock:
        latest_recent = latest_bond_on_or_before(_now().date(), max_gap_days=7)
        if _cache_is_fresh(latest_recent, BOND_CACHE_HOURS):
            logger.info("[BOND] 命中持久化缓存，跳过网络请求")
            return latest_recent
        if _cache_is_fresh(
            {"fetched_at": bot_data.get(BOND_FAILURE_CACHE_KEY)},
            BOND_CACHE_HOURS,
        ):
            return latest_bond_on_or_before(_now().date(), max_gap_days=36500)

        frame, source = await fetch_cn10y()
        if frame is not None:
            bot_data.pop(BOND_FAILURE_CACHE_KEY, None)
            count = persist_bond_rows(frame, source)
            logger.info("[BOND] 保存 %s 条 %s 收益率记录", count, source)
        else:
            bot_data[BOND_FAILURE_CACHE_KEY] = _now()
            logger.warning("[BOND] 刷新失败，使用本地已有国债收益率（如有）")
        return latest_bond_on_or_before(_now().date(), max_gap_days=36500)


async def backfill_cn10y() -> int:
    end_date = _now().date()
    earliest = db_execute(
        "SELECT MIN(valuation_date) AS valuation_date FROM benchmark_valuation_snapshots",
        fetchone=True,
    )
    start_date = (
        date.fromisoformat(earliest["valuation_date"]) - timedelta(days=7)
        if earliest and earliest["valuation_date"]
        else end_date - timedelta(days=30)
    )
    total = 0
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=349), end_date)
        frame = await _fetch_primary_bond(cursor, chunk_end)
        if frame is None:
            logger.warning("[BOND] 回填区间失败，继续下一个区间: %s至%s", cursor, chunk_end)
        else:
            total += persist_bond_rows(frame, "chinabond")
        cursor = chunk_end + timedelta(days=1)
        if cursor <= end_date and REQUEST_INTERVAL_SECONDS > 0:
            await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
    logger.info("[BOND] 历史回填完成，有效记录 %s 条", total)
    return total


def has_bond_history() -> bool:
    row = db_execute("SELECT 1 FROM macro_yield_snapshots LIMIT 1", fetchone=True)
    return row is not None
