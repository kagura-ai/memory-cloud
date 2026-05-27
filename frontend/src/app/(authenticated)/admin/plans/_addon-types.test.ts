/**
 * Tests for addon value validation (Issue #800).
 *
 * A legacy / broken cache can hold a value that is not a multiple of `perUnit`
 * (e.g. pre-#665 `addon_memory_bonus = 9000` with `perUnit = 10000`). The
 * backend rejects such values with HTTP 400 on save. Rather than render an
 * opaque 400, the dialog detects invalid values, warns the admin, and gates
 * Save until they are corrected.
 *
 * Design note (post code-review): `snapshotAddonValues` deliberately does NOT
 * coerce the stored value to 0. Coercing the editable form state was found to
 * (a) silently destroy a legacy value when the admin saves after editing an
 * unrelated field — the dialog sends all 9 fields — and (b) spuriously trigger
 * the LD-7 reduction warning on open. Instead the raw value is preserved (so
 * the dialog matches the read-only panel and nothing is lost), the warning is
 * driven by the live form state, and Save is disabled while any value is
 * invalid.
 */

import { describe, expect, it } from "vitest";

import type { WorkspaceQuotaDetail } from "@/lib/api/admin";
import {
  ADDON_TYPES,
  EMPTY_ADDON_VALUES,
  findInvalidAddonValues,
  isValidAddonValue,
  snapshotAddonValues,
  type AddonKey,
  type AddonValuesByKey,
} from "./_addon-types";

/** All-zero addon block, overridable per field. */
function makeQuota(
  addon: Partial<WorkspaceQuotaDetail["addon"]> = {},
): WorkspaceQuotaDetail {
  const zeroQuota = {
    memory_limit: 0,
    mcp_calls_per_day: 0,
    max_contexts: 0,
    max_members: 0,
    analysis_runs_per_day: 0,
    rest_calls_per_day: 0,
    public_calls_per_day: 0,
    storage_bytes_limit: 0,
    sleep_enabled_contexts_limit: 0,
    max_resource_tokens: 0,
  };
  return {
    workspace_id: "ws-test",
    workspace_name: "Test",
    plan_name: "pro",
    base: { ...zeroQuota },
    addon: {
      memory_bonus: 0,
      mcp_quota_bonus: 0,
      rest_quota_bonus: 0,
      public_quota_bonus: 0,
      member_bonus: 0,
      context_bonus: 0,
      analysis_bonus: 0,
      storage_bonus_mb: 0,
      sleep_contexts_bonus: 0,
      ...addon,
    },
    effective: { ...zeroQuota },
    usage: { memories: 0, contexts: 0, members: 0 },
    spend_cap: null,
  } as WorkspaceQuotaDetail;
}

/** Form values with a single key overridden. */
function values(overrides: Partial<AddonValuesByKey> = {}): AddonValuesByKey {
  return { ...EMPTY_ADDON_VALUES, ...overrides };
}

/** perUnit > 1 addons are the only ones where a non-multiple is possible. */
const SNAPPABLE = ADDON_TYPES.filter((m) => m.perUnit > 1);
/** perUnit === 1 addons (analysis, sleep): every non-negative integer is valid. */
const ALWAYS_VALID = ADDON_TYPES.filter((m) => m.perUnit === 1);

describe("isValidAddonValue", () => {
  it("accepts 0 (no addon is always saveable)", () => {
    expect(isValidAddonValue(SNAPPABLE[0], 0)).toBe(true);
  });

  it.each(SNAPPABLE)(
    "accepts an exact multiple of $key (perUnit=$perUnit)",
    (meta) => {
      expect(isValidAddonValue(meta, meta.perUnit * 3)).toBe(true);
    },
  );

  it.each(SNAPPABLE)("rejects a non-multiple of $key (perUnit-1)", (meta) => {
    expect(isValidAddonValue(meta, meta.perUnit - 1)).toBe(false);
  });

  it.each(ALWAYS_VALID)(
    "accepts any non-negative integer for perUnit=1 addon $key",
    (meta) => {
      expect(isValidAddonValue(meta, 7)).toBe(true);
    },
  );

  it("rejects negative values", () => {
    expect(isValidAddonValue(SNAPPABLE[0], -SNAPPABLE[0].perUnit)).toBe(false);
  });

  it("rejects NaN / non-integers", () => {
    expect(isValidAddonValue(SNAPPABLE[0], Number.NaN)).toBe(false);
    expect(isValidAddonValue(SNAPPABLE[0], 1.5)).toBe(false);
  });
});

describe("snapshotAddonValues", () => {
  it("preserves a valid multiple verbatim", () => {
    expect(
      snapshotAddonValues(makeQuota({ memory_bonus: 20_000 })).memory,
    ).toBe(20_000);
  });

  it("preserves a non-multiple verbatim — never silently coerces to 0", () => {
    // The dialog must show the admin their real stored value; saving is gated
    // separately. Coercing here would destroy the value on an unrelated save.
    expect(snapshotAddonValues(makeQuota({ memory_bonus: 9000 })).memory).toBe(
      9000,
    );
  });

  it("maps every addon key from its cache field", () => {
    const snapshot = snapshotAddonValues(
      makeQuota({
        memory_bonus: 10_000,
        storage_bonus_mb: 300,
        member_bonus: 5,
      }),
    );
    expect(snapshot.memory).toBe(10_000);
    expect(snapshot.storage).toBe(300);
    expect(snapshot.members).toBe(5);
  });
});

describe("findInvalidAddonValues", () => {
  it("returns [] when every value is a valid multiple", () => {
    expect(
      findInvalidAddonValues(values({ memory: 10_000, storage: 200 })),
    ).toEqual([]);
  });

  it("returns the key of a non-multiple value", () => {
    expect(findInvalidAddonValues(values({ memory: 9000 }))).toEqual<
      AddonKey[]
    >(["memory"]);
  });

  it("ignores zero (a valid, saveable amount)", () => {
    expect(findInvalidAddonValues(values({ memory: 0 }))).toEqual([]);
  });

  it("flags a negative value", () => {
    expect(findInvalidAddonValues(values({ members: -5 }))).toEqual<AddonKey[]>(
      ["members"],
    );
  });

  it("reports multiple broken keys in ADDON_TYPES order", () => {
    expect(
      findInvalidAddonValues(values({ memory: 9000, storage: 150 })),
    ).toEqual<AddonKey[]>(["memory", "storage"]);
  });
});
