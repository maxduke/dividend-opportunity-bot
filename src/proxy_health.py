"""Small, token-safe health adapter for the optional AKShare proxy."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from numbers import Real
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from . import config

logger = logging.getLogger(__name__)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

DISABLED = "DISABLED"
POSITIVE = "POSITIVE"
NO_BALANCE_OR_INVALID = "NO_BALANCE_OR_INVALID"
UNVERIFIED = "UNVERIFIED"
LOW_BALANCE = "LOW_BALANCE"
# Useful shorthand for callers while retaining the explicit state name.
NO_BALANCE = NO_BALANCE_OR_INVALID

ENABLE_AKSHARE_PROXY_PATCH = config.ENABLE_AKSHARE_PROXY_PATCH
AKSHARE_PROXY_AUTH_IP = config.AKSHARE_PROXY_AUTH_IP
AKSHARE_PROXY_AUTH_TOKEN = config.AKSHARE_PROXY_AUTH_TOKEN
AKSHARE_PROXY_BALANCE_CACHE_MINUTES = config.AKSHARE_PROXY_BALANCE_CACHE_MINUTES
AKSHARE_PROXY_LOW_BALANCE_THRESHOLD = config.AKSHARE_PROXY_LOW_BALANCE_THRESHOLD
ADMIN_USER_ID = config.ADMIN_USER_ID

_BALANCE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ProxyBalanceStatus:
    state: str
    balance: float | None
    checked_at: datetime
    reason: str | None = None


_lock = threading.RLock()
_cached_status: ProxyBalanceStatus | None = None
_cache_identity: tuple[bool, str, str, float] | None = None
_patch_active = False

# Public so tests and the command layer can inspect/reset the dedupe baseline.
last_notified_proxy_health_state: str | None = None


def _as_shanghai(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _status(
    state: str,
    checked_at: datetime,
    balance: float | None = None,
    reason: str | None = None,
) -> ProxyBalanceStatus:
    return ProxyBalanceStatus(state, balance, checked_at, reason)


def _fetch_balance(auth_ip: str, auth_token: str, checked_at: datetime) -> ProxyBalanceStatus:
    """Query the vendor endpoint without ever logging request data."""

    try:
        response = requests.get(
            f"http://{auth_ip}:47001/api/token/{quote(auth_token, safe='')}",
            timeout=_BALANCE_TIMEOUT_SECONDS,
        )
        status_code = response.status_code
        if 400 <= status_code < 500:
            return _status(NO_BALANCE_OR_INVALID, checked_at, reason="http_status")
        if status_code >= 500 or status_code != 200:
            return _status(UNVERIFIED, checked_at, reason="http_status")

        payload = response.json()
        balance_value = payload.get("balance") if isinstance(payload, dict) else None
        balance = _positive_number(balance_value)
        if balance is None:
            return _status(NO_BALANCE_OR_INVALID, checked_at, reason="invalid_balance")
        if balance > 0:
            return _status(POSITIVE, checked_at, balance=balance)
        return _status(NO_BALANCE_OR_INVALID, checked_at, balance=balance)
    except Exception:  # noqa: BLE001 - sanitize all provider exception text
        # Keep this log strictly static: requests errors can contain the URL/token.
        logger.warning("AKShare proxy balance check failed reason=request_error")
        return _status(UNVERIFIED, checked_at, reason="request_error")


def _setting_float(value: object, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _check_proxy_balance(
    *,
    force: bool = False,
    now: datetime | None = None,
    enabled: bool | None = None,
    auth_ip: str | None = None,
    auth_token: str | None = None,
    cache_minutes: float | None = None,
) -> ProxyBalanceStatus:
    """Implementation with optional startup overrides for bootstrap tests/config."""

    enabled = ENABLE_AKSHARE_PROXY_PATCH if enabled is None else bool(enabled)
    auth_ip = AKSHARE_PROXY_AUTH_IP if auth_ip is None else auth_ip
    auth_token = AKSHARE_PROXY_AUTH_TOKEN if auth_token is None else auth_token
    cache_minutes = (
        AKSHARE_PROXY_BALANCE_CACHE_MINUTES if cache_minutes is None else cache_minutes
    )
    cache_minutes = _setting_float(cache_minutes, 30.0)
    checked_at = _as_shanghai(now)
    identity = (enabled, str(auth_ip or ""), str(auth_token or ""), cache_minutes)

    global _cached_status, _cache_identity, _patch_active
    with _lock:
        if not enabled:
            status = _status(DISABLED, checked_at)
            _cached_status = status
            _cache_identity = identity
            _patch_active = False
            return status

        if not force and _cached_status and _cache_identity == identity:
            age = (checked_at - _cached_status.checked_at).total_seconds()
            if age < cache_minutes * 60:
                return _cached_status

        if not str(auth_ip or "").strip() or not str(auth_token or "").strip():
            status = _status(UNVERIFIED, checked_at, reason="missing_config")
        else:
            status = _fetch_balance(str(auth_ip).strip(), str(auth_token), checked_at)
        _cached_status = status
        _cache_identity = identity
        return status


def check_proxy_balance(
    force: bool = False, now: datetime | None = None
) -> ProxyBalanceStatus:
    """Return cached proxy health, refreshing it when stale or forced."""

    return _check_proxy_balance(force=force, now=now)


async def check_proxy_balance_async(
    force: bool = False, now: datetime | None = None
) -> ProxyBalanceStatus:
    return await asyncio.to_thread(check_proxy_balance, force=force, now=now)


def get_cached_proxy_balance_status() -> ProxyBalanceStatus | None:
    with _lock:
        return _cached_status


def proxy_patch_active() -> bool:
    with _lock:
        return _patch_active


def set_proxy_patch_active(active: bool) -> None:
    global _patch_active
    with _lock:
        _patch_active = bool(active)


def proxy_health_category(status: ProxyBalanceStatus | None = None) -> str | None:
    status = status or get_cached_proxy_balance_status()
    if status is None:
        return None
    threshold = _setting_float(AKSHARE_PROXY_LOW_BALANCE_THRESHOLD, 0.0)
    if (
        status.state == POSITIVE
        and threshold > 0
        and status.balance is not None
        and status.balance <= threshold
    ):
        return LOW_BALANCE
    return status.state


def next_balance_retry_at(status: ProxyBalanceStatus | None = None) -> datetime | None:
    status = status or get_cached_proxy_balance_status()
    if status is None or status.state == DISABLED:
        return None
    minutes = _setting_float(AKSHARE_PROXY_BALANCE_CACHE_MINUTES, 30.0)
    return status.checked_at + timedelta(minutes=minutes)


STARTUP_NO_BALANCE_MESSAGE = """⚠️ AKShare Proxy 不可用

检测到 akshare-proxy-patch 已配置，但当前积分不足或 Token 无效。

ETF 前复权历史数据可能无法获取，
MA200 / 52周回撤 / RSI6 将在必要时自动降级关闭。

请检查：
https://ak.cheapproxy.net

充值或修复 Token 后重启 Bot。"""

STARTUP_UNVERIFIED_MESSAGE = """⚠️ AKShare Proxy 状态无法验证

余额接口当前无法正常返回结果，因此本次启动未启用付费 proxy。

Bot 将继续运行，但 ETF 技术评分可能降级。

检查网络/服务状态后重启 Bot。"""

RUNTIME_NO_BALANCE_MESSAGE = """⚠️ AKShare Proxy 积分不足

技术数据源已进入降级模式。
ETF qfq history 暂停请求，技术评分可能不可用。

Bot 会定期重新检查余额。
充值后无需重建规则。"""

RECOVERY_MESSAGE = """✅ AKShare Proxy 已恢复

检测到积分恢复，ETF qfq history 将重新启用。
技术评分将在下一次成功获取历史数据后自动恢复。"""


def _low_balance_message(status: ProxyBalanceStatus) -> str:
    balance = status.balance if status.balance is not None else 0
    return (
        "⚠️ AKShare Proxy 余额偏低\n\n"
        f"当前余额：{balance:g}\n"
        "Proxy 仍可用，已达到配置的低余额提醒阈值。"
    )


async def notify_proxy_health(bot, startup: bool = False) -> bool:
    """Notify the administrator once per meaningful health transition."""

    status = get_cached_proxy_balance_status()
    if status is None:
        status = await check_proxy_balance_async()
    category = proxy_health_category(status)
    if category is None:
        return False

    global last_notified_proxy_health_state
    with _lock:
        previous = last_notified_proxy_health_state
        message = None
        if startup:
            if category == NO_BALANCE_OR_INVALID and previous != category:
                message = STARTUP_NO_BALANCE_MESSAGE
            elif category == UNVERIFIED and previous != category:
                message = STARTUP_UNVERIFIED_MESSAGE
            elif category == LOW_BALANCE and previous != category:
                message = _low_balance_message(status)
        elif category == NO_BALANCE_OR_INVALID:
            if previous != category and proxy_patch_active():
                message = RUNTIME_NO_BALANCE_MESSAGE
        elif category == POSITIVE:
            if previous in {NO_BALANCE_OR_INVALID, UNVERIFIED} and proxy_patch_active():
                message = RECOVERY_MESSAGE
        elif category == LOW_BALANCE:
            if previous in {NO_BALANCE_OR_INVALID, UNVERIFIED} and proxy_patch_active():
                message = RECOVERY_MESSAGE
            elif previous != category:
                message = _low_balance_message(status)
        last_notified_proxy_health_state = category

    if not message or not ADMIN_USER_ID:
        return False
    try:
        await bot.send_message(chat_id=ADMIN_USER_ID, text=message)
    except Exception as exc:  # noqa: BLE001 - Telegram implementation varies
        logger.warning(
            "AKShare proxy notification failed error_type=%s", type(exc).__name__
        )
        return False
    return True
