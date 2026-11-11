#!/usr/bin/env bats
# Pins the verdicts of .github/scripts/check-image-contents.sh (#2763): a clean
# api and web image pass; each thing the images must not carry fails and is
# named; a probe that cannot run is a failure, not an absence; an image that
# does not start and an unknown kind exit 2. docker is stubbed through the
# DOCKER env indirection, as wait_for_api.bats stubs curl.

SCRIPT="$BATS_TEST_DIRNAME/../check-image-contents.sh"

# docker run --rm --entrypoint <entrypoint> <image> <args...>
#   FAKE_PRESENT  names a probe finds: commands, `module` (the venv's Python)
#                 and `sys:module` (the system Python)
#   FAKE_BROKEN   a command whose probe fails the docker way (125)
#   FAKE_IMPORT_FAILS a present module whose import raises (exit 1)
#   FAKE_RUNNABLE=0 the image does not start; FAKE_APP_IMPORTS=0, FAKE_SERVER_JS=0
#   FAKE_UID, FAKE_USER, FAKE_NODE  what id -u, id -un and node print
mock_docker() {
  [ "$1 $2 $3" = "run --rm --entrypoint" ] || return 125
  local entrypoint="$4" name
  shift 5
  [ "${FAKE_RUNNABLE:-1}" = 1 ] || return 125
  if [ "$entrypoint" = sh ]; then
    [ "$2" = "exit 0" ] && return 0
    if [ "$2" = "test -f server.js" ]; then
      [ "${FAKE_SERVER_JS:-1}" = 1 ]
      return
    fi
    name="$4"
    [ "$name" = "${FAKE_BROKEN:-}" ] && return 125
    [[ " ${FAKE_PRESENT:-} " == *" $name "* ]] && return 0
    return 1
  fi
  if [ "$entrypoint" = python ] || [ "$entrypoint" = /usr/local/bin/python3 ]; then
    if [ "$2" = "import src.api.main" ]; then
      [ "${FAKE_APP_IMPORTS:-1}" = 1 ]
      return
    fi
    # A module probe must look the module up, not import it: an import of a
    # module in FAKE_IMPORT_FAILS exits 1 although the module is there.
    if [[ "$2" == *find_spec* ]]; then
      name="$3"
    else
      name="${2#import }"
      [[ " ${FAKE_IMPORT_FAILS:-} " == *" $name "* ]] && return 1
    fi
    [ "$entrypoint" = /usr/local/bin/python3 ] && name="sys:$name"
    [[ " ${FAKE_PRESENT:-} " == *" $name "* ]] && return 0
    return 1
  fi
  if [ "$entrypoint" = id ]; then
    if [ "$1" = -u ]; then echo "${FAKE_UID:-1000}"; else echo "${FAKE_USER:-nextjs}"; fi
    return 0
  fi
  [ "$entrypoint" = node ] && { echo "${FAKE_NODE:-24}"; return 0; }
  [ "$entrypoint" = alembic ] && return 0
  [ "$entrypoint" = curl ] && return 0
  return 127
}

setup() {
  TMP="$(mktemp -d)"
  export -f mock_docker
  export DOCKER=mock_docker
  echo "FROM node:24-alpine AS base" > "$TMP/Dockerfile"
  export WEB_DOCKERFILE="$TMP/Dockerfile"
}

teardown() {
  rm -rf "$TMP"
}

@test "a clean api image passes" {
  run "$SCRIPT" api img
  [ "$status" -eq 0 ]
  [[ "$output" == *"all checks passed for img"* ]]
  [[ "$output" != *"::error::"* ]]
}

@test "the dev extra in the api image fails and is named" {
  FAKE_PRESENT="pytest ruff" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"pytest (dev extra) is in the image"* ]]
  [[ "$output" == *"ruff is in the image"* ]]
  [[ "$output" == *"2 check(s) failed for img"* ]]
}

@test "a compiler in the api image fails" {
  FAKE_PRESENT="gcc" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"gcc is in the image"* ]]
}

@test "a present module whose import raises still fails the check" {
  FAKE_PRESENT="pytest" FAKE_IMPORT_FAILS="pytest" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"pytest (dev extra) is in the image"* ]]
}

@test "a C++ compiler or clang in the api image fails" {
  FAKE_PRESENT="g++ clang" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"g++ is in the image"* ]]
  [[ "$output" == *"clang is in the image"* ]]
}

@test "uv left in the api image fails" {
  FAKE_PRESENT="uv" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"uv is in the image"* ]]
}

@test "the base image's pip fails, in the system Python only" {
  FAKE_PRESENT="sys:pip" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"pip (system Python) is in the image"* ]]
  # The same name in the venv is not what this check is about.
  FAKE_PRESENT="pip" run "$SCRIPT" api img
  [ "$status" -eq 0 ]
}

@test "an api image running as root fails" {
  FAKE_UID=0 run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"the user id is '0' (exit 0), expected '1000'"* ]]
}

@test "an api image whose application does not import fails" {
  FAKE_APP_IMPORTS=0 run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"the application imports failed (exit 1)"* ]]
}

@test "a probe that fails the docker way is a failure, not an absence" {
  FAKE_BROKEN="gcc" run "$SCRIPT" api img
  [ "$status" -eq 1 ]
  [[ "$output" == *"could not check gcc (exit 125)"* ]]
}

@test "a clean web image passes" {
  run "$SCRIPT" web img
  [ "$status" -eq 0 ]
  [[ "$output" == *"Node's major is 24"* ]]
  [[ "$output" == *"all checks passed for img"* ]]
}

@test "a package manager in the web image fails and is named" {
  FAKE_PRESENT="npm corepack" run "$SCRIPT" web img
  [ "$status" -eq 1 ]
  [[ "$output" == *"npm is in the image"* ]]
  [[ "$output" == *"corepack is in the image"* ]]
}

@test "a web image as another user or without server.js fails" {
  FAKE_USER=root FAKE_SERVER_JS=0 run "$SCRIPT" web img
  [ "$status" -eq 1 ]
  [[ "$output" == *"the user is 'root' (exit 0), expected 'nextjs'"* ]]
  [[ "$output" == *"server.js is present failed (exit 1)"* ]]
}

@test "a web image on another Node major fails" {
  FAKE_NODE=22 run "$SCRIPT" web img
  [ "$status" -eq 1 ]
  [[ "$output" == *"Node's major is '22' (exit 0), expected '24'"* ]]
}

@test "the Node major is read from a digest-pinned base too" {
  echo "FROM node:26-alpine@sha256:$(printf 'a%.0s' {1..64}) AS base" > "$WEB_DOCKERFILE"
  FAKE_NODE=26 run "$SCRIPT" web img
  [ "$status" -eq 0 ]
  [[ "$output" == *"Node's major is 26"* ]]
}

@test "the real frontend/Dockerfile names a Node major the script can read" {
  unset WEB_DOCKERFILE
  major="$(sed -nE 's/^FROM node:([0-9]+)-alpine.* AS base$/\1/p' "$BATS_TEST_DIRNAME/../../../frontend/Dockerfile")"
  [ -n "$major" ]
  FAKE_NODE="$major" run "$SCRIPT" web img
  [ "$status" -eq 0 ]
}

@test "a Dockerfile without a node base fails the web check" {
  echo "FROM debian AS base" > "$WEB_DOCKERFILE"
  run "$SCRIPT" web img
  [ "$status" -eq 1 ]
  [[ "$output" == *"no 'FROM node:<major>-alpine AS base'"* ]]
}

@test "an image that does not start exits 2" {
  FAKE_RUNNABLE=0 run "$SCRIPT" api img
  [ "$status" -eq 2 ]
  [[ "$output" == *"cannot run img (exit 125)"* ]]
}

@test "an unknown kind exits 2 with the usage" {
  run "$SCRIPT" worker img
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: check-image-contents.sh api|web <image>"* ]]
}
