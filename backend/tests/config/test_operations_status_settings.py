"""Operations status settings (#2768).

The probe makes paid calls, so it is off by default; when on, an interval
shorter than one run's worst case (four targets x 20 s) is refused at boot.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import OPERATIONS_PROBE_MIN_INTERVAL_SECONDS, Settings

_ENV_KEYS = ["OPERATIONS_PROBE_INTERVAL_SECONDS", "OPERATIONS_STATUS_OVERDUE_GRACE_SECONDS"]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults_probe_off_grace_fifteen_minutes(clean_env):
    s = Settings(_env_file=None)
    assert s.operations_probe_interval_seconds == 0
    assert s.operations_status_overdue_grace_seconds == 900
    assert OPERATIONS_PROBE_MIN_INTERVAL_SECONDS == 300


@pytest.mark.parametrize("value", ["0", "300", "900"])
def test_accepted_intervals(clean_env, value):
    clean_env.setenv("OPERATIONS_PROBE_INTERVAL_SECONDS", value)
    assert Settings(_env_file=None).operations_probe_interval_seconds == int(value)


@pytest.mark.parametrize("value", ["1", "60", "299", "-1"])
def test_refused_intervals(clean_env, value):
    clean_env.setenv("OPERATIONS_PROBE_INTERVAL_SECONDS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_grace_is_read_and_never_negative(clean_env):
    clean_env.setenv("OPERATIONS_STATUS_OVERDUE_GRACE_SECONDS", "0")
    assert Settings(_env_file=None).operations_status_overdue_grace_seconds == 0
    clean_env.setenv("OPERATIONS_STATUS_OVERDUE_GRACE_SECONDS", "-5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
