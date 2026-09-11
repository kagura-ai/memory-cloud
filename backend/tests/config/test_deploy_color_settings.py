"""DEPLOY_COLOR / ACTIVE_COLOR_MARKER_PATH validation (#1482)."""

import pytest
from pydantic import ValidationError

from config.constants import DEPLOY_COLORS
from config.settings import Settings


def test_defaults_are_uncolored_and_point_at_the_container_mount(monkeypatch):
    monkeypatch.delenv("DEPLOY_COLOR", raising=False)
    monkeypatch.delenv("ACTIVE_COLOR_MARKER_PATH", raising=False)
    s = Settings(_env_file=None)
    assert s.deploy_color == ""
    assert s.active_color_marker_path == "/run/kagura/active-color"


@pytest.mark.parametrize(("raw", "expected"), [("blue", "blue"), (" Green\n", "green")])
def test_known_colors_are_normalized(monkeypatch, raw, expected):
    monkeypatch.setenv("DEPLOY_COLOR", raw)
    assert Settings(_env_file=None).deploy_color == expected
    assert expected in DEPLOY_COLORS


def test_unknown_color_refuses_to_start(monkeypatch):
    # responding_color exists to expose a mismatch; a typo here would make it lie.
    monkeypatch.setenv("DEPLOY_COLOR", "purple")
    with pytest.raises(ValidationError, match="DEPLOY_COLOR"):
        Settings(_env_file=None)
