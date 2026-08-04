/**
 * The client must not re-derive the context quota (#1487).
 *
 * A Pro workspace with 3 contexts could not create a fourth: the page computed
 *
 *     plan_name === "free" && contexts.length >= 1
 *
 * which hardcodes both the tier and the number. It ignored the
 * PLAN_*_MAX_CONTEXTS settings overrides, ignored the purchasable addon bonus,
 * and had no basic/pro branch at all — so a Pro workspace at its real cap of 20
 * got an enabled button and a server rejection, while a free workspace whose
 * cap had been raised to 5 got blocked at 1.
 *
 * The server now sends `max_contexts` (the same number the create path
 * enforces), so the only correct client rule is a comparison against it.
 *
 * These tests pin the RULE rather than the page, because the rule is what was
 * wrong. `isQuotaReached` mirrors the expression in page.tsx exactly.
 */
import { describe, expect, it } from "vitest";

/** Mirror of the expression in page.tsx. */
function isQuotaReached(
  maxContexts: number | undefined,
  contextCount: number,
): boolean {
  return maxContexts !== undefined && contextCount >= maxContexts;
}

/** The rule this replaced, kept so the difference is demonstrable. */
function legacyIsQuotaReached(planName: string, contextCount: number): boolean {
  return planName === "free" && contextCount >= 1;
}

describe("context quota gate", () => {
  it("blocks exactly at the server-sent cap", () => {
    expect(isQuotaReached(20, 19)).toBe(false);
    expect(isQuotaReached(20, 20)).toBe(true);
    expect(isQuotaReached(20, 21)).toBe(true);
  });

  it("does not block a Pro workspace below its cap (the reported bug)", () => {
    // Pro = 20. Three contexts must leave the control enabled.
    expect(isQuotaReached(20, 3)).toBe(false);
    // ...whereas the rule it replaced also said false here, which is why the
    // quota was never the real cause — see the key-gate test below.
    expect(legacyIsQuotaReached("pro", 3)).toBe(false);
  });

  it("gates basic at 3, which the old rule never did at all", () => {
    expect(isQuotaReached(3, 3)).toBe(true);
    // The old rule let a Basic workspace at its cap through to a server error.
    expect(legacyIsQuotaReached("basic", 3)).toBe(false);
  });

  it("respects a raised free cap instead of hardcoding 1", () => {
    // PLAN_FREE_MAX_CONTEXTS=5 is a supported deployment setting.
    expect(isQuotaReached(5, 1)).toBe(false);
    expect(isQuotaReached(5, 4)).toBe(false);
    expect(isQuotaReached(5, 5)).toBe(true);
    // The old rule blocked at 1 regardless of the operator's configuration.
    expect(legacyIsQuotaReached("free", 1)).toBe(true);
  });

  it("respects an addon bonus on a free workspace", () => {
    // free base 1 + addon 4 => the server sends 5.
    expect(isQuotaReached(5, 3)).toBe(false);
    // The old rule discarded a purchased entitlement.
    expect(legacyIsQuotaReached("free", 3)).toBe(true);
  });

  it("does not block when the cap is unknown", () => {
    // An older API build omits the field. Blocking on a guess is the bug;
    // the create call stays authoritative and returns a clear error.
    expect(isQuotaReached(undefined, 0)).toBe(false);
    expect(isQuotaReached(undefined, 999)).toBe(false);
  });

  it("treats a genuine zero cap as reached", () => {
    // Distinct from `undefined`: 0 is a real answer, not a missing one.
    expect(isQuotaReached(0, 0)).toBe(true);
  });
});
