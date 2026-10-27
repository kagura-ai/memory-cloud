"""``config.plan_tiers`` no longer builds the settings at import (#2276).

``get_settings()`` applies the plan-tier env overrides when it first builds
the settings. Run as subprocesses: inside this pytest process the settings
are already built and the overrides applied.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import config.plan_tiers as plan_tiers

_BACKEND = Path(__file__).resolve().parents[2]
_SECRETS = ("API_KEY_SECRET", "AUDIT_HMAC_KEY", "AUDIT_PSEUDO_SALT")
_RESOURCES_ON_BASIC = (
    "api_keys,oauth,reranking,managed_embeddings,secret_store,resources,connectors,public_contexts"
)


def _run(code: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLAN_")}
    env |= {"ENV_FILE": str(_BACKEND / "nonexistent.env"), "PYTHONPATH": str(_BACKEND / "src")}
    env |= overrides
    return subprocess.run(
        [sys.executable, "-c", code + "print('ok')\n"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr
    # The override pass logs to stdout before the marker.
    assert result.stdout.strip().splitlines()[-1] == "ok"


def test_importing_the_module_does_not_build_the_settings() -> None:
    # A refused production install: building the settings would raise.
    env = dict.fromkeys(_SECRETS, "") | {"ENVIRONMENT": "production"}
    result = _run(
        "import config.plan_tiers as p, config.settings as s\n"
        "assert s._settings is None\n"
        "assert not p._overrides_applied\n"
        "assert p.PLAN_TIERS['free'].memory_limit == p.PLAN_FREE.memory_limit\n"
        "assert p.PLAN_ORDER == ('free', 'basic', 'pro', 'promax')\n",
        **env,
    )
    _assert_ok(result)


def test_building_the_settings_applies_the_overrides_to_bound_names() -> None:
    result = _run(
        "from config.plan_tiers import FEATURE_MIN_PLANS, PLAN_TIERS\n"
        "from config.settings import get_settings\n"
        "assert PLAN_TIERS['free'].memory_limit != 4242\n"
        "get_settings()\n"
        "assert PLAN_TIERS['free'].memory_limit == 4242\n"
        "assert FEATURE_MIN_PLANS['resources'] == 'basic'\n",
        PLAN_FREE_MEMORY_LIMIT="4242",
        PLAN_BASIC_FEATURES=_RESOURCES_ON_BASIC,
    )
    _assert_ok(result)


def test_a_bad_features_override_refuses_every_get_settings_call() -> None:
    result = _run(
        "import config.settings as s\n"
        "for _ in range(2):\n"
        "    try:\n"
        "        s.get_settings()\n"
        "    except ValueError as exc:\n"
        "        assert 'PLAN_FREE_FEATURES' in str(exc)\n"
        "    else:\n"
        "        raise SystemExit('no ValueError')\n"
        "    assert s._settings is None\n",
        PLAN_FREE_FEATURES="api_keys,no_such_feature",
    )
    _assert_ok(result)


def test_the_overrides_are_applied_once_per_process() -> None:
    result = _run(
        "import config.plan_tiers as p, config.settings as s\n"
        "s.get_settings()\n"
        "first = p.PLAN_TIERS['free']\n"
        "s._settings = None\n"
        "s.get_settings()\n"
        "assert p.PLAN_TIERS['free'] is first\n",
        PLAN_FREE_MEMORY_LIMIT="4242",
    )
    _assert_ok(result)


def test_the_registries_are_plain_dicts() -> None:
    assert type(plan_tiers.PLAN_TIERS) is dict
    assert type(plan_tiers.FEATURE_MIN_PLANS) is dict
