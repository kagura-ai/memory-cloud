/**
 * Tests for addon snap + non-multiple detection (Issue #800).
 *
 * `snapshotAddonValues` populates the admin addon dialog from the quota
 * detail response. A legacy / broken cache can hold a value that is not a
 * multiple of `perUnit` (e.g. pre-#665 `addon_memory_bonus = 9000` with
 * `perUnit = 10000`). The backend rejects such values with HTTP 400 on save,
 * so the dialog must snap them and warn the admin rather than render the
 * invalid value verbatim and surface an opaque 400.
 *
 * Snap policy (#800-A): reset non-multiples to 0 (not round) so the admin's
 * value is never silently rewritten — paired with a visible warning driven by
 * `detectNonMultipleAddons`.
 */

import { describe, expect, it } from "vitest";

import type { WorkspaceQuotaDetail } from "@/lib/api/admin";
import {
  ADDON_TYPES,
  detectNonMultipleAddons,
  snapshotAddonValues,
  type AddonKey,
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

/** perUnit > 1 addons are the only ones where a non-multiple is possible. */
const SNAPPABLE = ADDON_TYPES.filter((m) => m.perUnit > 1);
/** perUnit === 1 addons (analysis, sleep): every integer is a valid multiple. */
const ALWAYS_VALID = ADDON_TYPES.filter((m) => m.perUnit === 1);

describe("snapshotAddonValues", () => {
  it("snaps a non-multiple memory_bonus (9000) to 0", () => {
    const snapshot = snapshotAddonValues(makeQuota({ memory_bonus: 9000 }));
    expect(snapshot.memory).toBe(0);
  });

  it("preserves a valid multiple verbatim", () => {
    const snapshot = snapshotAddonValues(makeQuota({ memory_bonus: 20_000 }));
    expect(snapshot.memory).toBe(20_000);
  });

  it.each(SNAPPABLE)(
    "snaps a non-multiple $key value to 0 (perUnit=$perUnit)",
    (meta) => {
      const snapshot = snapshotAddonValues(
        makeQuota({ [meta.addonField]: meta.perUnit - 1 }),
      );
      expect(snapshot[meta.key]).toBe(0);
    },
  );

  it.each(SNAPPABLE)(
    "preserves a valid multiple of $key (2 * perUnit)",
    (meta) => {
      const snapshot = snapshotAddonValues(
        makeQuota({ [meta.addonField]: meta.perUnit * 2 }),
      );
      expect(snapshot[meta.key]).toBe(meta.perUnit * 2);
    },
  );

  it.each(ALWAYS_VALID)(
    "preserves any value for perUnit=1 addon $key",
    (meta) => {
      const snapshot = snapshotAddonValues(makeQuota({ [meta.addonField]: 7 }));
      expect(snapshot[meta.key]).toBe(7);
    },
  );

  it("leaves an all-valid snapshot untouched", () => {
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

describe("detectNonMultipleAddons", () => {
  it("returns [] when every addon value is a valid multiple", () => {
    expect(
      detectNonMultipleAddons(
        makeQuota({ memory_bonus: 10_000, storage_bonus_mb: 200 }),
      ),
    ).toEqual([]);
  });

  it("returns the key of a non-zero non-multiple value", () => {
    expect(detectNonMultipleAddons(makeQuota({ memory_bonus: 9000 }))).toEqual([
      "memory",
    ]);
  });

  it("ignores zero values (zero is a valid multiple, nothing to warn about)", () => {
    expect(detectNonMultipleAddons(makeQuota({ memory_bonus: 0 }))).toEqual([]);
  });

  it("reports multiple broken keys in ADDON_TYPES order", () => {
    const result = detectNonMultipleAddons(
      makeQuota({ memory_bonus: 9000, storage_bonus_mb: 150 }),
    );
    expect(result).toEqual<AddonKey[]>(["memory", "storage"]);
  });
});
