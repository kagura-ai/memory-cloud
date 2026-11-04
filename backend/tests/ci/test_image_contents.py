"""Guard tests for #2457 and #2620: what the images carry next to the application.

A scan of the api and web images reports what ships beside the application as
well as its dependencies, so the images leave out what they do not run:

- web: the package managers of the Node base image. The runner only runs
  ``node server.js``, and its health check uses ``node -e``.
- api: the base image's ``pip``, ``setuptools`` and ``wheel``, which sit outside
  ``.venv``, and the ``uv`` binary, which only builds the image. The shipped
  stage also leaves out the dev extra and a compiler (#2620): the tests run in
  the ``dev`` stage, which only the dev compose file builds.

Node itself is pinned in the image, the dev compose file and CI. They name one
LTS major: CI on another Node than the image proves little.

No CI job builds the images, so these tests are the only thing that notices a
step going missing. They read Dockerfile instructions, not text: a step that is
commented out must fail here.
"""

import re
from pathlib import Path

import pytest
import yaml

# backend/tests/ci/test_image_contents.py -> ci -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

# As in test_uv_version_lockstep.py: the Dockerfiles and workflows are not
# copied into the image, so the guard runs from a repository checkout.
pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)

_OLDEST_NODE = 24
_WEB_REMOVED = (
    "/usr/local/lib/node_modules",
    "/opt/yarn-*",
    "/usr/local/bin/npm",
    "/usr/local/bin/npx",
    "/usr/local/bin/corepack",
    "/usr/local/bin/yarn",
    "/usr/local/bin/yarnpkg",
)
_WEB_CHECKED_GONE = ("npm", "corepack", "yarn")
# The production compose files build the api without a `target`, which builds
# the last stage of backend/Dockerfile.
_API_SHIPPED = "runtime"
_API_PRODUCTION_COMPOSE = (
    "terraform/single-server/docker-compose.prod.yml",
    "terraform/single-server/docker-compose.app.yml",
)
_COMPILERS = ("build-essential", "gcc", "g++", "clang")


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _instructions(rel: str) -> list[str]:
    """A Dockerfile's instructions, one string each: comments gone, continuations joined."""
    lines = [line for line in _read(rel).splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n\s*", " ", "\n".join(lines))
    return [line for line in joined.splitlines() if line.strip()]


def _stages(rel: str) -> dict[str, tuple[str, list[str]]]:
    """A Dockerfile's stages in order: name -> (what it starts FROM, its instructions)."""
    stages: dict[str, tuple[str, list[str]]] = {}
    for line in _instructions(rel):
        if line.startswith("FROM "):
            match = re.fullmatch(r"FROM (\S+) AS (\S+)", line)
            assert match, f"{rel}: every stage needs a name: {line}"
            stages[match.group(2)] = (match.group(1), [])
        elif stages:
            list(stages.values())[-1][1].append(line)
    return stages


def _api_stage(target: str) -> list[str]:
    """Every instruction that builds ``target`` of the api image, its parent stages first."""
    stages = _stages("backend/Dockerfile")
    assert target in stages, f"backend/Dockerfile has no stage {target!r}"
    chain: list[str] = []
    while target in stages:
        parent, instructions = stages[target]
        chain[:0] = instructions
        target = parent
    return chain


def _web_runner() -> list[str]:
    """The ``runner`` stage of the web image: the only stage that ships."""
    instructions = _instructions("frontend/Dockerfile")
    return instructions[instructions.index("FROM base AS runner") + 1 :]


def _web_runner_runs() -> str:
    return "\n".join(line for line in _web_runner() if line.startswith("RUN "))


def _node_image_major() -> str:
    match = re.search(
        r"^FROM node:(\d+)-alpine AS base$",
        "\n".join(_instructions("frontend/Dockerfile")),
        re.MULTILINE,
    )
    assert match, "frontend/Dockerfile must start from a node:<major>-alpine base"
    return match.group(1)


def _setup_node_versions() -> list[str]:
    """``node-version`` of every setup-node step, whatever form it is written in."""
    versions = []
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for step in job.get("steps") or []:
                if "actions/setup-node" in str(step.get("uses")):
                    versions.append(str((step.get("with") or {}).get("node-version")))
    return versions


def test_node_is_a_current_lts_line() -> None:
    major = int(_node_image_major())
    assert major >= _OLDEST_NODE
    assert major % 2 == 0, "odd Node majors are short-lived and never become LTS"


def test_node_major_is_the_same_in_the_image_compose_and_ci() -> None:
    major = _node_image_major()
    compose = yaml.safe_load(_read("docker-compose.yml"))
    assert compose["services"]["web"]["image"] == f"node:{major}-alpine"
    versions = _setup_node_versions()
    assert versions, "no workflow sets up Node"
    assert set(versions) == {major}


@pytest.mark.parametrize("path", _WEB_REMOVED)
def test_web_runner_removes_the_package_managers(path: str) -> None:
    assert path in _web_runner_runs()


@pytest.mark.parametrize("tool", _WEB_CHECKED_GONE)
def test_web_build_fails_if_a_package_manager_survives(tool: str) -> None:
    # `rm -rf` succeeds on a path that has moved, so the build checks the result.
    assert f"! command -v {tool}" in _web_runner_runs()


def test_web_runner_upgrades_alpine_packages() -> None:
    assert "apk upgrade --no-cache" in _web_runner_runs()


def test_web_runner_starts_and_probes_without_a_removed_tool() -> None:
    commands = [
        line for line in _web_runner() if line.startswith(("CMD", "ENTRYPOINT", "HEALTHCHECK"))
    ]
    assert 'CMD ["node", "server.js"]' in commands
    assert any(line.startswith("HEALTHCHECK") for line in commands)
    for command in commands:
        assert not re.search(r"\b(npm|npx|corepack|yarn)\b", command), command


def test_api_image_ships_its_last_stage() -> None:
    assert list(_stages("backend/Dockerfile"))[-1] == _API_SHIPPED


@pytest.mark.parametrize("rel", _API_PRODUCTION_COMPOSE)
def test_production_compose_builds_the_api_default_target(rel: str) -> None:
    services = yaml.safe_load(_read(rel))["services"]
    builds = [
        service["build"]
        for service in services.values()
        if isinstance(service.get("build"), dict)
        and service["build"]["context"].rstrip("/").endswith("backend")
    ]
    assert builds, f"{rel} builds no api image"
    for build in builds:
        assert "target" not in build, f"{rel} must ship the last stage, not {build['target']!r}"


def test_api_image_installs_no_extra_in_the_shipped_stage() -> None:
    syncs = [line for line in _api_stage(_API_SHIPPED) if "uv sync" in line]
    assert syncs
    for sync in syncs:
        assert "--extra" not in sync and "--all-extras" not in sync, sync


def test_api_image_installs_no_compiler() -> None:
    for target in _stages("backend/Dockerfile"):
        installs = [line for line in _api_stage(target) if "apt-get install" in line]
        packages = {word for line in installs for word in line.split()}
        assert not packages & set(_COMPILERS), f"{target}: every locked package has a wheel"


def test_dev_compose_builds_the_api_dev_stage_with_the_dev_extra() -> None:
    compose = yaml.safe_load(_read("docker-compose.yml"))
    assert compose["services"]["api"]["build"]["target"] == "dev"
    # `make test` runs pytest inside that container.
    assert any("uv sync" in line and "--extra dev" in line for line in _api_stage("dev"))


def test_api_image_removes_base_packaging_tools_as_root() -> None:
    instructions = _api_stage(_API_SHIPPED)
    uninstall = instructions.index(
        "RUN pip uninstall -y --root-user-action=ignore setuptools wheel pip"
    )
    assert uninstall < instructions.index("USER kagura"), "needs root: site-packages is root's"


@pytest.mark.parametrize("target", ["dev", _API_SHIPPED])
def test_api_image_removes_uv_after_its_last_use_and_ends_unprivileged(target: str) -> None:
    instructions = _api_stage(target)
    # A plain rm: if uv moves, the build fails instead of quietly keeping it.
    removal = instructions.index("RUN rm /bin/uv /bin/uvx")
    assert max(i for i, line in enumerate(instructions) if "uv sync" in line) < removal
    assert instructions[removal - 1] == "USER root"
    assert [line for line in instructions if line.startswith("USER ")][-1] == "USER kagura"
