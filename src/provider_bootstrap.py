"""Install optional data-provider integrations before AKShare is imported."""

from __future__ import annotations

import logging

from .config import (
    AKSHARE_PROXY_AUTH_IP,
    AKSHARE_PROXY_AUTH_TOKEN,
    AKSHARE_PROXY_HOOK_DOMAINS,
    AKSHARE_PROXY_RETRY,
    ENABLE_AKSHARE_PROXY_PATCH,
)

logger = logging.getLogger(__name__)
_installed = False


def install_data_provider_patch() -> bool:
    """Enable the opt-in EastMoney proxy patch before provider modules load."""
    global _installed
    if _installed or not ENABLE_AKSHARE_PROXY_PATCH:
        return _installed
    if not AKSHARE_PROXY_AUTH_IP or not AKSHARE_PROXY_AUTH_TOKEN:
        raise RuntimeError(
            "AKShare proxy patch enabled but AKSHARE_PROXY_AUTH_IP/TOKEN is missing"
        )
    if AKSHARE_PROXY_RETRY < 1:
        raise RuntimeError("AKShare proxy retry must be at least 1")
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
