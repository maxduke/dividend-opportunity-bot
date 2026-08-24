"""Install optional data-provider integrations before AKShare is imported."""

from __future__ import annotations

import logging

from . import proxy_health
from .config import (
    AKSHARE_PROXY_AUTH_IP,
    AKSHARE_PROXY_AUTH_TOKEN,
    AKSHARE_PROXY_HOOK_DOMAINS,
    AKSHARE_PROXY_RETRY,
    ENABLE_AKSHARE_PROXY_PATCH,
)

logger = logging.getLogger(__name__)
_installed = False


def _sync_health_settings() -> None:
    """Keep bootstrap's patchable config view aligned with the health adapter."""
    proxy_health.ENABLE_AKSHARE_PROXY_PATCH = ENABLE_AKSHARE_PROXY_PATCH
    proxy_health.AKSHARE_PROXY_AUTH_IP = AKSHARE_PROXY_AUTH_IP
    proxy_health.AKSHARE_PROXY_AUTH_TOKEN = AKSHARE_PROXY_AUTH_TOKEN


def install_data_provider_patch() -> bool:
    """Enable the opt-in EastMoney proxy patch before provider modules load."""
    global _installed
    _sync_health_settings()
    if not ENABLE_AKSHARE_PROXY_PATCH:
        _installed = False
        proxy_health.check_proxy_balance(force=True)
        proxy_health.set_proxy_patch_active(False)
        return False
    if _installed:
        return True
    if not AKSHARE_PROXY_AUTH_IP or not AKSHARE_PROXY_AUTH_TOKEN:
        raise RuntimeError(
            "AKShare proxy patch enabled but AKSHARE_PROXY_AUTH_IP/TOKEN is missing"
        )
    if AKSHARE_PROXY_RETRY < 1:
        raise RuntimeError("AKShare proxy retry must be at least 1")

    balance = proxy_health.check_proxy_balance(force=True)
    if balance.state != proxy_health.POSITIVE:
        proxy_health.set_proxy_patch_active(False)
        logger.warning(
            "[AKSHARE] proxy patch skipped; balance_status=%s reason=%s",
            balance.state,
            balance.reason,
        )
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
    proxy_health.set_proxy_patch_active(True)
    logger.info(
        "[AKSHARE] proxy patch enabled for %d EastMoney domains; retry=%d; fast=false",
        len(hook_domains),
        AKSHARE_PROXY_RETRY,
    )
    return True
