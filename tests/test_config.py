import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHECK_INTERVAL_SECONDS", "１２"),
        ("VALUATION_CACHE_HOURS", "nan"),
        ("BOND_CACHE_HOURS", "inf"),
        ("ENABLE_INTRADAY_MONITOR", "yes"),
        ("ENABLE_AKSHARE_PROXY_PATCH", "1"),
        ("FETCH_FAILURE_THRESHOLD", "0"),
        ("FETCH_RETRY_DELAY_SECONDS", "-1"),
        ("DAILY_BRIEFING_TIMES", "25:00"),
        ("DAILY_BRIEFING_TIMES", "a:b"),
    ],
)
def test_invalid_config_is_reported_without_traceback(name, value):
    environment = os.environ.copy()
    environment.update(
        {
            "TELEGRAM_TOKEN": "test-token",
            "ADMIN_USER_ID": "12345",
            name: value,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "from src.config import validate_config; validate_config()"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "配置错误" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("briefing_times", ["", "9:30,1:2"])
def test_valid_briefing_times_are_accepted(briefing_times):
    environment = os.environ.copy()
    environment.update(
        {
            "TELEGRAM_TOKEN": "test-token",
            "ADMIN_USER_ID": "12345",
            "DAILY_BRIEFING_TIMES": briefing_times,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "from src.config import validate_config; validate_config()"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
