"""Install optional data-provider integrations before AKShare is imported."""

from __future__ import annotations

import logging
import math
from numbers import Real
from urllib.parse import quote

import requests

from .config import (
    AKSHARE_PROXY_AUTH_IP,
    AKSHARE_PROXY_AUTH_TOKEN,
    AKSHARE_PROXY_HOOK_DOMAINS,
    AKSHARE_PROXY_RETRY,
    ENABLE_AKSHARE_PROXY_PATCH,
)

logger = logging.getLogger(__name__)
_installed = False
_BALANCE_TIMEOUT_SECONDS = 5


def install_data_provider_patch() -> bool:
    """Enable the opt-in EastMoney proxy patch before provider modules load."""
    global _installed
    if _installed or not ENABLE_AKSHARE_PROXY_PATCH:
        return _installed
    if not AKSHARE_PROXY_AUTH_IP or not AKSHARE_PROXY_AUTH_TOKEN:
        raise RuntimeError(
            "AKShare proxy patch enabled but AKSHARE_PROXY_AUTH_IP/TOKEN is missing"
        )
    if not _has_positive_balance():
        logger.warning("[AKSHARE] proxy patch not enabled: balance unavailable or non-positive")
        return False

    try:
        import akshare_proxy_patch
    except ImportError as exc:
        raise RuntimeError(
            "AKShare proxy patch enabled but akshare-proxy-patch is not installed"
        ) from exc

    hook_domains = [domain.strip() for domain in AKSHARE_PROXY_HOOK_DOMAINS.split(",") if domain.strip()]
    akshare_proxy_patch.install_patch(
        AKSHARE_PROXY_AUTH_IP,
        auth_token=AKSHARE_PROXY_AUTH_TOKEN,
        retry=AKSHARE_PROXY_RETRY,
        hook_domains=hook_domains,
        # Concurrent pagination can multiply paid requests. This bot only
        # needs bounded per-symbol calls, so match fund-alert-bot and disable it.
        fast=False,
    )
    _installed = True
    logger.info(
        "[AKSHARE] proxy patch enabled for %d EastMoney domains; retry=%d; fast=false",
        len(hook_domains),
        AKSHARE_PROXY_RETRY,
    )
    return True


def _has_positive_balance() -> bool:
    """Fail closed without logging the reusable token."""
    try:
        response = requests.get(
            f"http://{AKSHARE_PROXY_AUTH_IP}:47001/api/token/"
            f"{quote(AKSHARE_PROXY_AUTH_TOKEN, safe='')}",
            timeout=_BALANCE_TIMEOUT_SECONDS,
        )
        payload = response.json() if 200 <= response.status_code < 300 else None
        balance = payload.get("balance") if isinstance(payload, dict) else None
        return (
            isinstance(balance, Real)
            and not isinstance(balance, bool)
            and math.isfinite(float(balance))
            and balance > 0
        )
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        return False
