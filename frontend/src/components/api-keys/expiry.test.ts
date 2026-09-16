/**
 * Expiry selection → request value (#1537).
 *
 * The server now treats an omitted/null `expires_days` as "use the deployment
 * default", so the dialog must send an explicit `0` for "Never" — sending
 * null there would silently produce a 365-day key.
 */

import { describe, it, expect } from "vitest";
import {
  DEFAULT_EXPIRY_SELECTION,
  NEVER_EXPIRY_SELECTION,
  expiresDaysFromSelection,
} from "./expiry";

describe("expiresDaysFromSelection", () => {
  it("defaults the dialog to the server default (365 days)", () => {
    expect(DEFAULT_EXPIRY_SELECTION).toBe("365");
    expect(expiresDaysFromSelection(DEFAULT_EXPIRY_SELECTION)).toBe(365);
  });

  it("sends an explicit 0 for Never — null would mean 'server default'", () => {
    expect(expiresDaysFromSelection(NEVER_EXPIRY_SELECTION)).toBe(0);
  });

  it("passes numeric selections through", () => {
    expect(expiresDaysFromSelection("30")).toBe(30);
    expect(expiresDaysFromSelection("90")).toBe(90);
  });
});
