#!/usr/bin/env bats
# =============================================================================
# Drift guard for the Qdrant client/server pair (#1535, #1793).
#
# qdrant-client warns on every start-up unless its major matches the server's
# and the minors differ by at most 1. The server image is pinned as a literal
# in several compose / workflow files, the client range lives in
# backend/pyproject.toml and the version CI and the image install is the one
# backend/uv.lock resolves. Nothing in YAML or TOML ties the three together;
# this suite does, so bumping one side without the other fails CI instead of
# printing "Qdrant client version X is incompatible with server version Y" on
# every API start. Static conformance — no docker, no network.
# =============================================================================

REPO_ROOT="$BATS_TEST_DIRNAME/../../../.."

# The sites the pyproject comment names. A new site is caught by the first
# test (it greps every tracked YAML file); add it here too so the floor holds.
IMAGE_FILES=(
    "docker-compose.yml"
    ".github/workflows/eval-nightly.yml"
    "terraform/single-server/docker-compose.prod.yml"
    "terraform/single-server/docker-compose.data.yml"
)

# Every distinct Qdrant tag the tracked YAML files reference outside comments,
# one per line. Quotes, a registry prefix, a digest (@sha256:...) and a
# trailing comment are not part of the tag.
server_tag() {
    local pat="qdrant/qdrant:[^\"'[:space:]@#]+"
    git -C "$REPO_ROOT" grep -hE 'qdrant/qdrant:' -- '*.yml' '*.yaml' \
        | grep -vE '^[[:space:]]*#' \
        | grep -oE "$pat" | sed -E 's/^qdrant\/qdrant://' | sort -u
}

@test "every tracked YAML file pins the same Qdrant image tag" {
    run server_tag
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [[ "${lines[0]}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "each listed site pins the image (guard not vacuous)" {
    cd "$REPO_ROOT"
    for f in "${IMAGE_FILES[@]}"; do
        run grep -cE "^[[:space:]]*image:[[:space:]]*[\"']?([a-z0-9.-]+/)?qdrant/qdrant:v[0-9]+\.[0-9]+\.[0-9]+(@sha256:[0-9a-f]{64})?[\"']?[[:space:]]*(#.*)?$" "$f"
        [ "$status" -eq 0 ]
        [ "$output" -ge 1 ]
    done
}

# Prints OK, or the reason the client range / lock falls outside the window of
# the server tag given as $1.
check_window() {
    (cd "$REPO_ROOT" && python3 - "$1" <<'PYEOF'
import re
import sys
import tomllib

tag = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", sys.argv[1])
if not tag:
    sys.exit(f"unexpected server tag {sys.argv[1]!r}")
s_major, s_minor = int(tag[1]), int(tag[2])

with open("backend/pyproject.toml", "rb") as fh:
    deps = tomllib.load(fh)["project"]["dependencies"]
specs = [
    d.split(";")[0].replace(" ", "")  # drop environment markers and spaces
    for d in deps
    if re.match(r"qdrant-client\s*(\[|>|<|=|~|!|;|$)", d)
]
if len(specs) != 1:
    sys.exit(f"expected one qdrant-client requirement, found {specs}")
rng = re.fullmatch(r"qdrant-client(?:\[[^\]]*\])?>=(\d+)\.(\d+)\.(\d+),<(\d+)\.(\d+)", specs[0])
if not rng:
    sys.exit(f"expected a '>=X.Y.Z,<X.Y' range, found {specs[0]!r}")
lo_major, lo_minor, _, hi_major, hi_minor = map(int, rng.groups())
if not lo_major == hi_major == s_major:
    sys.exit(f"client major {lo_major}/{hi_major} != server major {s_major}")
minors = range(lo_minor, hi_minor)
if not minors:
    sys.exit(f"empty client range {specs[0]!r}")
outside = [m for m in minors if abs(m - s_minor) > 1]
if outside:
    sys.exit(f"client minors {outside} are more than one minor from server {sys.argv[1]}")

with open("backend/uv.lock", "rb") as fh:
    locked = [p["version"] for p in tomllib.load(fh)["package"] if p["name"] == "qdrant-client"]
if len(locked) != 1:
    sys.exit(f"expected one locked qdrant-client, found {locked}")
v = re.match(r"(\d+)\.(\d+)\.", locked[0])
if not v or int(v[1]) != s_major or int(v[2]) not in minors:
    sys.exit(f"uv.lock resolves qdrant-client {locked[0]}, outside {specs[0]}")
print("OK")
PYEOF
    )
}

@test "the client range and the lock stay within one minor of the server" {
    tag="$(server_tag)"
    [ -n "$tag" ]
    run check_window "$tag"
    echo "$output"
    [ "$status" -eq 0 ]
    [ "$output" = "OK" ]
}

@test "the window check rejects a server tag three minors past the pinned one" {
    # Self-test: proves check_window can fail, so the test above is not vacuous.
    tag="$(server_tag)"
    major="$(echo "$tag" | cut -d. -f1)"
    minor="$(echo "$tag" | cut -d. -f2)"
    run check_window "${major}.$((minor + 3)).0"
    [ "$status" -ne 0 ]
    [[ "$output" == *"more than one minor"* ]]
}
