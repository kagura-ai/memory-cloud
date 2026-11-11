"""Guard tests for #2771: the workflow that publishes server.json to the MCP Registry.

``.github/workflows/publish-mcp-registry.yml`` runs with ``id-token: write`` and
its first real run is a release tag push, so nobody can try a publish before it
counts. What can drift without anyone noticing is pinned here:

- it runs on ``v*`` tags and by hand only, never on a pull request;
- the token is read-only for the repository; the one job adds ``id-token: write``
  and nothing else, and only this repository runs it (a fork's tag does nothing);
- every action is pinned by a commit SHA, the ``mcp-publisher`` release by its
  version and archive digest, and the archive is verified with cosign before it
  is unpacked;
- the version guard runs before the download, and ``login`` and ``publish`` run
  only on a tag ref (a dispatch on a branch is a dry run).
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

# backend/tests/ci/test_mcp_registry_publish.py -> ci -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "publish-mcp-registry.yml"

# As in test_image_contents.py: workflows are not copied into the image.
pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)

_ON_TAG = "github.ref_type == 'tag'"
_SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["publish"]
    return jobs["publish"]


def _steps() -> list[dict[str, Any]]:
    return _job()["steps"]


def _index(predicate_text: str) -> int:
    """The position of the first step whose ``run`` or ``uses`` contains the text."""
    for i, step in enumerate(_steps()):
        if predicate_text in str(step.get("run", "")) + str(step.get("uses", "")):
            return i
    raise AssertionError(f"no step runs or uses {predicate_text!r}")


def test_triggers_are_v_tags_and_dispatch_only() -> None:
    workflow = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}


def test_permissions_are_read_only_plus_id_token_on_the_job() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}
    # A job's permissions replace the top-level ones, so contents: read is
    # repeated; the only addition is the OIDC token.
    assert _job()["permissions"] == {"contents": "read", "id-token": "write"}


def test_only_this_repository_runs_the_job() -> None:
    assert _job()["if"] == "github.repository == 'kagura-ai/memory-cloud'"


def test_runs_are_serialised_and_bounded() -> None:
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert isinstance(_job()["timeout-minutes"], int)


def test_no_secrets_are_read() -> None:
    assert "secrets." not in _WORKFLOW.read_text(encoding="utf-8")


def test_every_action_is_pinned_by_a_commit_sha() -> None:
    uses = [step["uses"] for step in _steps() if "uses" in step]
    assert uses
    for ref in uses:
        assert _SHA_PIN.match(ref), ref


def test_checkout_does_not_keep_the_token() -> None:
    checkout = _steps()[_index("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False


def test_the_checkout_pin_is_the_one_ci_uses() -> None:
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    pins = set(re.findall(r"actions/checkout@[0-9a-f]{40}", ci))
    assert len(pins) == 1
    assert _steps()[_index("actions/checkout@")]["uses"] in pins


def test_publisher_is_pinned_by_version_and_digest() -> None:
    env = _job()["env"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", env["MCP_PUBLISHER_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["MCP_PUBLISHER_SHA256"])
    assert "releases/latest" not in _WORKFLOW.read_text(encoding="utf-8")


def test_publisher_is_verified_before_it_is_unpacked() -> None:
    download = str(_steps()[_index("cosign verify-blob")]["run"])
    identity = (
        "--certificate-identity "
        '"https://github.com/modelcontextprotocol/registry/.github/workflows/release.yml'
        '@refs/tags/${MCP_PUBLISHER_VERSION}"'
    )
    assert identity in download
    assert '--certificate-oidc-issuer "https://token.actions.githubusercontent.com"' in download
    assert download.index("sha256sum --check") < download.index("tar ")
    assert download.index("cosign verify-blob") < download.index("tar ")
    assert _index("sigstore/cosign-installer@") < _index("cosign verify-blob")


def test_the_version_guard_runs_before_the_download() -> None:
    guard = _index(".github/scripts/check-server-json-version.sh")
    assert guard < _index("cosign verify-blob")
    assert guard > _index("actions/checkout@")


def test_validate_runs_before_login_and_publish() -> None:
    validate = _index('mcp-publisher" validate')
    assert (
        _index("cosign verify-blob")
        < validate
        < _index("login github-oidc")
        < _index('mcp-publisher" publish')
    )


@pytest.mark.parametrize("command", ['mcp-publisher" login github-oidc', 'mcp-publisher" publish'])
def test_login_and_publish_run_on_a_tag_ref_only(command: str) -> None:
    step = _steps()[_index(command)]
    assert step["if"] == _ON_TAG


def test_the_dry_run_is_decided_by_the_ref_not_an_input() -> None:
    # A mistyped input must not be able to turn a dry run into a publish.
    workflow = _workflow()
    assert not workflow.get("on", workflow.get(True))["workflow_dispatch"]
    assert "inputs." not in _WORKFLOW.read_text(encoding="utf-8")


def test_the_summary_runs_whatever_happened() -> None:
    summary = _steps()[-1]
    assert summary["if"] == "always()"
    assert "GITHUB_STEP_SUMMARY" in str(summary["run"])
