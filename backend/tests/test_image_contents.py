"""Guard tests for #2457: what the images carry next to the application.

A scan of the api and web images reports what ships beside the application as
well as its dependencies, so the images leave out what they do not run:

- web: the package managers of the Node base image. The runner only runs
  ``node server.js``, and its health check uses ``node -e``.
- api: the base image's ``setuptools`` and ``wheel``, which sit outside
  ``.venv``, and the ``uv`` binary, which only builds the image.

Node itself is pinned in the image, the dev compose file and CI. They name one
supported major version: CI on another Node than the image proves little.
"""

import re
from pathlib import Path

import pytest

# backend/tests/test_image_contents.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# As in test_uv_version_lockstep.py: the Dockerfiles and workflows are not
# copied into the image, so the guard runs from a repository checkout.
pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)

_OLDEST_SUPPORTED_NODE = 22
_WEB_REMOVED = (
    "/usr/local/lib/node_modules",
    "/opt/yarn-*",
    "/usr/local/bin/npm",
    "/usr/local/bin/npx",
    "/usr/local/bin/corepack",
    "/usr/local/bin/yarn",
    "/usr/local/bin/yarnpkg",
)


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _web_runner() -> str:
    """The ``runner`` stage of the web image: the only stage that ships."""
    _, _, runner = _read("frontend/Dockerfile").partition("FROM base AS runner")
    assert runner, "frontend/Dockerfile has no runner stage"
    return runner


def _node_image_major() -> str:
    match = re.search(
        r"^FROM node:(\d+)-alpine AS base$", _read("frontend/Dockerfile"), re.MULTILINE
    )
    assert match, "frontend/Dockerfile must start from a node:<major>-alpine base"
    return match.group(1)


def test_node_is_a_supported_major() -> None:
    assert int(_node_image_major()) >= _OLDEST_SUPPORTED_NODE


def test_node_major_is_the_same_in_the_image_compose_and_ci() -> None:
    major = _node_image_major()
    assert re.findall(r"image: node:(\d+)-alpine", _read("docker-compose.yml")) == [major]
    in_ci = re.findall(r'node-version: "(\d+)"', _read(".github/workflows/ci.yml"))
    assert in_ci, "ci.yml sets up no Node"
    assert set(in_ci) == {major}


@pytest.mark.parametrize("path", _WEB_REMOVED)
def test_web_runner_removes_the_package_managers(path: str) -> None:
    assert path in _web_runner()


def test_web_runner_upgrades_alpine_packages() -> None:
    assert "apk upgrade --no-cache" in _web_runner()


def test_web_runner_does_not_run_a_removed_tool() -> None:
    commands = [
        line for line in _web_runner().splitlines() if line.startswith(("CMD", "ENTRYPOINT"))
    ]
    assert commands == ['CMD ["node", "server.js"]']


def test_api_image_removes_base_setuptools_and_wheel_as_root() -> None:
    dockerfile = _read("backend/Dockerfile")
    uninstall = dockerfile.index("pip uninstall -y --root-user-action=ignore setuptools wheel")
    assert uninstall < dockerfile.index("USER kagura"), "needs root: site-packages is root's"


def test_api_image_removes_uv_after_its_last_use_and_ends_unprivileged() -> None:
    dockerfile = _read("backend/Dockerfile")
    removal = dockerfile.index("rm -f /bin/uv /bin/uvx")
    assert dockerfile.rfind("uv sync") < removal
    assert dockerfile.rfind("USER root", 0, removal) > dockerfile.rfind("USER kagura", 0, removal)
    assert re.findall(r"^USER (\S+)$", dockerfile, re.MULTILINE)[-1] == "kagura"
