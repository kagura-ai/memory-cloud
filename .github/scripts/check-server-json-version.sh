#!/usr/bin/env bash
# Checks that a release tag and server.json name the same version (#2771).
#
# .github/workflows/publish-mcp-registry.yml runs this before it downloads
# mcp-publisher: the registry listing must carry the version the tag released.
# backend/tests/test_release_version_lockstep.py already ties server.json to
# APP_VERSION; this ties it to the tag that triggered the run.
#
# server.json is checked first, so a dry run on a branch still reports what it
# would publish. Exit codes:
#   0  the ref is v<version> and <version> is server.json's `version`
#   1  server.json is missing, not JSON, has no string `version`, or differs
#   2  no ref given
#   3  the ref is not a v-tag (a dispatch on a branch: the dry run allows this)
#
# Usage: check-server-json-version.sh <ref-name>
# Env:   SERVER_JSON (default server.json at the root of this checkout).
set -euo pipefail

usage="usage: check-server-json-version.sh <ref-name>"
if [ "$#" -lt 1 ]; then
  echo "$usage" >&2
  exit 2
fi
ref="$1"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
server_json="${SERVER_JSON:-$repo_root/server.json}"

if [ ! -f "$server_json" ]; then
  echo "::error::$server_json not found"
  exit 1
fi
if ! jq empty "$server_json" > /dev/null 2>&1; then
  echo "::error::$server_json is not valid JSON"
  exit 1
fi
version="$(jq -r 'if type == "object" and (.version | type) == "string" then .version else "" end' "$server_json")"
if [ -z "$version" ]; then
  echo "::error::$server_json has no string 'version'"
  exit 1
fi

case "$ref" in
  v*) ;;
  *)
    echo "::error::ref '$ref' is not a v-tag; server.json version is $version"
    exit 3
    ;;
esac

tag_version="${ref#v}"
if [ "$tag_version" != "$version" ]; then
  echo "::error::tag $ref is version $tag_version, but server.json says $version"
  exit 1
fi
echo "ok: tag $ref matches server.json version $version"
