"""``config.plan_tiers`` builds the settings on first use, not at import (#2276).

Run as subprocesses: inside this pytest process the settings and the
registries are already built.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from config.plan_tiers import FEATURE_MIN_PLANS, PLAN_ORDER, PLAN_TIERS

_BACKEND = Path(__file__).resolve().parents[2]
_SECRETS = ("API_KEY_SECRET", "AUDIT_HMAC_KEY", "AUDIT_PSEUDO_SALT")


def _run(code: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLAN_")}
    env |= {"ENV_FILE": str(_BACKEND / "nonexistent.env"), "PYTHONPATH": str(_BACKEND / "src")}
    env |= overrides
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_importing_the_module_does_not_build_the_settings() -> None:
    # A refused production install: building the settings would raise.
    env = dict.fromkeys(_SECRETS, "") | {"ENVIRONMENT": "production"}
    result = _run(
        "import config.plan_tiers as p, config.settings as s\n"
        "assert s._settings is None\n"
        "assert not p._overrides_applied\n"
        "assert p.PLAN_ORDER == ('free', 'basic', 'pro', 'promax')\n"
        "assert 'secret_store' in p.KNOWN_FEATURES\n"
        "print('ok')\n",
        **env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok"


def test_the_first_read_applies_the_overrides() -> None:
    result = _run(
        "from config.plan_tiers import FEATURE_MIN_PLANS, PLAN_TIERS\n"
        "import config.settings as s\n"
        "assert PLAN_TIERS['free'].memory_limit == 4242\n"
        "assert s._settings is not None\n"
        "assert FEATURE_MIN_PLANS['resources'] == 'basic'\n"
        "print('ok')\n",
        PLAN_FREE_MEMORY_LIMIT="4242",
        PLAN_BASIC_FEATURES=(
            "api_keys,oauth,reranking,managed_embeddings,secret_store,"
            "resources,connectors,public_contexts"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok"


def test_a_bad_features_override_fails_every_use() -> None:
    result = _run(
        "import config.plan_tiers as p\n"
        "for _ in range(2):\n"
        "    try:\n"
        "        p.PLAN_TIERS.get('free')\n"
        "    except ValueError as exc:\n"
        "        assert 'PLAN_FREE_FEATURES' in str(exc)\n"
        "    else:\n"
        "        raise SystemExit('no ValueError')\n"
        "print('ok')\n",
        PLAN_FREE_FEATURES="api_keys,no_such_feature",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok"


def test_the_registries_stay_plain_dicts_to_callers() -> None:
    assert isinstance(PLAN_TIERS, dict)
    assert tuple(PLAN_TIERS) == PLAN_ORDER
    assert dict(PLAN_TIERS) == {name: PLAN_TIERS[name] for name in PLAN_ORDER}
    assert {**FEATURE_MIN_PLANS} == FEATURE_MIN_PLANS.copy()
