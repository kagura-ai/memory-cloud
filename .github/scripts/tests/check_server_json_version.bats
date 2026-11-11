#!/usr/bin/env bats
# Pins the verdicts of .github/scripts/check-server-json-version.sh (#2771): a
# v-tag whose version equals server.json's `version` passes; a missing file,
# unparsable JSON, a missing or non-string `version`, a mismatch and a ref that
# is not a v-tag each fail with their own message. A ref that is not a v-tag
# exits 3 (the dry run tolerates that one exit only); every other failure
# exits 1, a missing argument 2.

SCRIPT="$BATS_TEST_DIRNAME/../check-server-json-version.sh"

setup() {
  TMP="$(mktemp -d)"
  export SERVER_JSON="$TMP/server.json"
  echo '{"name": "io.github.example/server", "version": "1.2.3"}' > "$SERVER_JSON"
}

teardown() {
  rm -rf "$TMP"
}

@test "a v-tag equal to server.json's version passes" {
  run "$SCRIPT" v1.2.3
  [ "$status" -eq 0 ]
  [[ "$output" == *"ok: tag v1.2.3 matches server.json version 1.2.3"* ]]
  [[ "$output" != *"::error::"* ]]
}

@test "a tag that differs from server.json's version fails" {
  run "$SCRIPT" v1.2.4
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::tag v1.2.4 is version 1.2.4, but server.json says 1.2.3"* ]]
}

@test "a pre-release suffix is a mismatch, not a match" {
  run "$SCRIPT" v1.2.3-rc1
  [ "$status" -eq 1 ]
  [[ "$output" == *"but server.json says 1.2.3"* ]]
}

@test "a missing server.json fails" {
  rm "$SERVER_JSON"
  run "$SCRIPT" v1.2.3
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::$SERVER_JSON not found"* ]]
}

@test "unparsable JSON fails" {
  echo '{"version": "1.2.3",' > "$SERVER_JSON"
  run "$SCRIPT" v1.2.3
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::$SERVER_JSON is not valid JSON"* ]]
}

@test "a server.json without a version fails" {
  echo '{"name": "io.github.example/server"}' > "$SERVER_JSON"
  run "$SCRIPT" v1.2.3
  [ "$status" -eq 1 ]
  [[ "$output" == *"::error::$SERVER_JSON has no string 'version'"* ]]
}

@test "a non-string or empty version fails like a missing one" {
  echo '{"version": 1.2}' > "$SERVER_JSON"
  run "$SCRIPT" v1.2
  [ "$status" -eq 1 ]
  [[ "$output" == *"has no string 'version'"* ]]
  echo '{"version": ""}' > "$SERVER_JSON"
  run "$SCRIPT" v
  [ "$status" -eq 1 ]
  [[ "$output" == *"has no string 'version'"* ]]
  echo '[]' > "$SERVER_JSON"
  run "$SCRIPT" v1.2.3
  [ "$status" -eq 1 ]
  [[ "$output" == *"has no string 'version'"* ]]
}

@test "a ref that is not a v-tag exits 3 and still reports server.json's version" {
  run "$SCRIPT" main
  [ "$status" -eq 3 ]
  [[ "$output" == *"::error::ref 'main' is not a v-tag; server.json version is 1.2.3"* ]]
  run "$SCRIPT" 1.2.3
  [ "$status" -eq 3 ]
}

@test "a broken server.json fails before the ref is looked at" {
  echo 'not json' > "$SERVER_JSON"
  run "$SCRIPT" main
  [ "$status" -eq 1 ]
  [[ "$output" == *"is not valid JSON"* ]]
}

@test "a missing ref exits 2 with the usage" {
  run "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: check-server-json-version.sh <ref-name>"* ]]
}

@test "the real server.json passes against its own version" {
  unset SERVER_JSON
  version="$(jq -r .version "$BATS_TEST_DIRNAME/../../../server.json")"
  run "$SCRIPT" "v$version"
  [ "$status" -eq 0 ]
}
