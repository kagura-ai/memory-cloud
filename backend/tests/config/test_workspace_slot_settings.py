"""WORKSPACE_SLOT_SUSPENSION and its accessor (#2918)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from config.settings import Settings
from services.workspace_slots import workspace_slot_mode, workspace_slots_enforced


def test_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("WORKSPACE_SLOT_SUSPENSION", raising=False)
    assert Settings(_env_file=None).workspace_slot_suspension == "off"


@pytest.mark.parametrize(
    ("raw", "expected"), [("off", "off"), ("shadow", "shadow"), (" shadow\n", "shadow")]
)
def test_off_and_shadow_need_nothing_else(monkeypatch, raw, expected):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", raw)
    monkeypatch.delenv("ENFORCE_WORKSPACE_CAP", raising=False)
    assert Settings(_env_file=None).workspace_slot_suspension == expected


def test_enforce_with_the_workspace_cap_enforced_is_accepted(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", "enforce")
    monkeypatch.setenv("ENFORCE_WORKSPACE_CAP", "true")
    assert Settings(_env_file=None).workspace_slot_suspension == "enforce"


def test_enforce_without_the_workspace_cap_enforced_refuses_to_start(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", "enforce")
    monkeypatch.setenv("ENFORCE_WORKSPACE_CAP", "false")
    with pytest.raises(ValidationError, match="ENFORCE_WORKSPACE_CAP=true"):
        Settings(_env_file=None)


@pytest.mark.parametrize("raw", ["", "  ", "Enforce", "OFF", "on", "true", "suspend"])
def test_empty_unknown_or_miscased_mode_refuses_to_start(monkeypatch, raw):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", raw)
    monkeypatch.setenv("ENFORCE_WORKSPACE_CAP", "true")
    with pytest.raises(ValidationError, match="workspace_slot_suspension"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("mode", "enforced"), [("off", False), ("shadow", False), ("enforce", True)]
)
def test_accessor_reads_the_setting_at_call_time(mode, enforced):
    fake = SimpleNamespace(workspace_slot_suspension=mode)
    with patch("config.settings.get_settings", return_value=fake):
        assert workspace_slot_mode() == mode
        assert workspace_slots_enforced() is enforced


def test_the_grant_defaults_match_the_tiers():
    from config.plan_tiers import PLAN_TIERS
    from config.settings import _DEFAULT_OWNED_WORKSPACE_GRANTS

    assert list(_DEFAULT_OWNED_WORKSPACE_GRANTS) == list(PLAN_TIERS)
    assert {k: PLAN_TIERS[k].owned_workspace_grant for k in PLAN_TIERS} == (
        _DEFAULT_OWNED_WORKSPACE_GRANTS
    )


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_grants_decreasing_along_the_plan_order_refuse_to_start(monkeypatch, mode):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", mode)
    monkeypatch.setenv("ENFORCE_WORKSPACE_CAP", "true")
    monkeypatch.setenv("PLAN_PRO_OWNED_WORKSPACE_GRANT", "5")
    monkeypatch.setenv("PLAN_PROMAX_OWNED_WORKSPACE_GRANT", "3")
    with pytest.raises(ValidationError, match="PLAN_PROMAX_OWNED_WORKSPACE_GRANT"):
        Settings(_env_file=None)


def test_decreasing_grants_are_only_refused_while_the_feature_is_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", "off")
    monkeypatch.setenv("PLAN_PRO_OWNED_WORKSPACE_GRANT", "5")
    monkeypatch.setenv("PLAN_PROMAX_OWNED_WORKSPACE_GRANT", "3")
    assert Settings(_env_file=None).plan_promax_owned_workspace_grant == 3


@pytest.mark.parametrize(("pro", "promax"), [("2", "19"), ("4", "4"), ("0", "0")])
def test_non_decreasing_grants_are_accepted(monkeypatch, pro, promax):
    monkeypatch.setenv("WORKSPACE_SLOT_SUSPENSION", "enforce")
    monkeypatch.setenv("ENFORCE_WORKSPACE_CAP", "true")
    monkeypatch.setenv("PLAN_PRO_OWNED_WORKSPACE_GRANT", pro)
    monkeypatch.setenv("PLAN_PROMAX_OWNED_WORKSPACE_GRANT", promax)
    assert Settings(_env_file=None).workspace_slot_suspension == "enforce"
