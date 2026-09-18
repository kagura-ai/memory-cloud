/**
 * Expiry selection → request value (#1537).
 *
 * The server treats an omitted/null `expires_days` as "use the deployment
 * default", so the dialog's default selection must send `null` (not a
 * literal 365, which would bypass a tightened `API_KEY_DEFAULT_EXPIRES_DAYS`)
 * and "Never" must send an explicit `0`.
 */

import { describe, it, expect } from "vitest";
import {
  DEFAULT_EXPIRY_SELECTION,
  NEVER_EXPIRY_SELECTION,
  SERVER_DEFAULT_EXPIRY_SELECTION,
  expiresDaysFromSelection,
} from "./expiry";

describe("expiresDaysFromSelection", () => {
  it("defaults the dialog to the server default, sent as null", () => {
    expect(DEFAULT_EXPIRY_SELECTION).toBe(SERVER_DEFAULT_EXPIRY_SELECTION);
    expect(expiresDaysFromSelection(DEFAULT_EXPIRY_SELECTION)).toBeNull();
  });

  it("keeps 1 year available as an explicit choice distinct from the default", () => {
    expect(expiresDaysFromSelection("365")).toBe(365);
  });

  it("sends an explicit 0 for Never — null would mean 'server default'", () => {
    expect(expiresDaysFromSelection(NEVER_EXPIRY_SELECTION)).toBe(0);
  });

  it("passes numeric selections through", () => {
    expect(expiresDaysFromSelection("30")).toBe(30);
    expect(expiresDaysFromSelection("90")).toBe(90);
  });
});
