/**
 * parseBetaInviteInput (#1655) — what the /login "I have an invite link"
 * entry accepts: a bare token or a pasted /join/<token> link.
 */
import { describe, expect, it } from "vitest";

import {
  BETA_INVITE_TOKEN_PATTERN,
  isBetaInviteToken,
  parseBetaInviteInput,
} from "./betaInviteToken";

const TOKEN = "AbC_def-0123456789xyzQ";

describe("BETA_INVITE_TOKEN_PATTERN", () => {
  it("mirrors the backend pattern ^[A-Za-z0-9_-]{20,128}$", () => {
    expect(BETA_INVITE_TOKEN_PATTERN.source).toBe("^[A-Za-z0-9_-]{20,128}$");
    expect(isBetaInviteToken("a".repeat(20))).toBe(true);
    expect(isBetaInviteToken("a".repeat(128))).toBe(true);
    expect(isBetaInviteToken("a".repeat(19))).toBe(false);
    expect(isBetaInviteToken("a".repeat(129))).toBe(false);
    expect(isBetaInviteToken(`${"a".repeat(20)}.`)).toBe(false);
  });
});

describe("parseBetaInviteInput", () => {
  it.each([
    ["a bare token", TOKEN],
    ["a bare token with whitespace", `  ${TOKEN}\n`],
    ["an absolute link", `https://app.example/join/${TOKEN}`],
    ["a link with a trailing slash", `https://app.example/join/${TOKEN}/`],
    ["a link with its own query and hash", `https://app.example/join/${TOKEN}?return_to=%2Fx#y`],
    ["a relative link", `/join/${TOKEN}`],
  ])("accepts %s", (_label, value) => {
    expect(parseBetaInviteInput(value)).toBe(TOKEN);
  });

  it.each([
    ["empty", ""],
    ["a short token", "abc"],
    ["a token with a dot", `${TOKEN}.x`],
    ["another path", `https://app.example/invite/${TOKEN}`],
    ["a nested path", `https://app.example/join/${TOKEN}/extra`],
    ["an encoded token segment", `https://app.example/join/${TOKEN}%2F..`],
    ["a token only in the query", `https://app.example/login?invite=${TOKEN}`],
    ["a token only in return_to", `/login?return_to=%2Fjoin%2F${TOKEN}`],
    ["javascript:", `javascript:/join/${TOKEN}`],
  ])("rejects %s", (_label, value) => {
    expect(parseBetaInviteInput(value)).toBeNull();
  });
});
