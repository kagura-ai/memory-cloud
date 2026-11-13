"""Upload malware scan settings (#565): default off, bounded values.

With every new setting at its default the scanner is off and nothing about the
upload or download path changes; the values that are set are checked at boot
so a typo fails loudly instead of turning into a scanner that never runs.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings

_SCAN_ENV_KEYS = (
    "FILE_SCAN_ENABLED",
    "CLAMD_HOST",
    "CLAMD_PORT",
    "CLAMD_CONNECT_TIMEOUT_SECONDS",
    "CLAMD_SCAN_TIMEOUT_SECONDS",
    "CLAMD_STREAM_MAX_BYTES",
    "FILE_SCAN_CONCURRENCY",
    "FILE_SCAN_MAX_ATTEMPTS",
    "FILE_SCAN_BACKFILL_BATCH",
    "FILE_QUARANTINE_RETENTION_DAYS",
    "FILE_SCAN_PUT_EXPIRY_MARGIN_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _SCAN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_leave_the_scanner_off():
    s = Settings()
    assert s.file_scan_enabled is False
    assert s.clamd_host == "clamav"
    assert s.clamd_port == 3310
    assert s.clamd_connect_timeout_seconds == 5.0
    assert s.clamd_scan_timeout_seconds == 300.0
    assert s.clamd_stream_max_bytes == 100 * 1024 * 1024
    assert s.file_scan_concurrency == 2
    assert s.file_scan_max_attempts == 3
    assert s.file_scan_backfill_batch == 20
    assert s.file_quarantine_retention_days == 7
    assert s.file_scan_put_expiry_margin_seconds == 30


def test_env_names(monkeypatch):
    monkeypatch.setenv("FILE_SCAN_ENABLED", "true")
    monkeypatch.setenv("CLAMD_HOST", "scanner.internal")
    monkeypatch.setenv("CLAMD_PORT", "3311")
    monkeypatch.setenv("FILE_SCAN_BACKFILL_BATCH", "0")
    s = Settings()
    assert s.file_scan_enabled is True
    assert s.clamd_host == "scanner.internal"
    assert s.clamd_port == 3311
    assert s.file_scan_backfill_batch == 0


def test_enabled_needs_a_host():
    with pytest.raises(ValidationError, match="CLAMD_HOST"):
        Settings(file_scan_enabled=True, clamd_host="  ")
    # Off: an empty host is not used, so it is not an error.
    assert Settings(file_scan_enabled=False, clamd_host="").clamd_host == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("clamd_port", 0),
        ("clamd_port", 65536),
        ("clamd_connect_timeout_seconds", 0),
        ("clamd_scan_timeout_seconds", -1),
        ("clamd_stream_max_bytes", 0),
        ("file_scan_concurrency", 0),
        ("file_scan_max_attempts", 0),
        ("file_scan_backfill_batch", -1),
        ("file_quarantine_retention_days", 0),
        ("file_scan_put_expiry_margin_seconds", -1),
    ],
)
def test_out_of_range_values_fail_at_boot(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})
