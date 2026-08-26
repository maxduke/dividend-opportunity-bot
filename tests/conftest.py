# -*- coding: utf-8 -*-

import os
from unittest.mock import AsyncMock

import pytest

# 设置测试所需的最小环境变量，避免 config.py 导入时报错
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("ADMIN_USER_ID", "12345")


@pytest.fixture(autouse=True)
def mock_calendar_preload(monkeypatch):
    preload = AsyncMock()
    monkeypatch.setattr("src.jobs.ensure_trade_days_loaded", preload)
    monkeypatch.setattr("src.opportunity.ensure_trade_days_loaded", preload)
    return preload
