/**
 * Addon metadata for the admin plans page (Issue #663).
 *
 * Single source of truth for the 9 quota dimensions that the admin can
 * adjust via the addon dialog. Both the quota detail panel (read-only
 * row table) and the addon edit dialog iterate over `ADDON_TYPES` so a
 * new addon type only requires adding one entry here plus two i18n
 * keys (`admin.plans.quota.<key>` and `admin.plans.addonDialog.<key>`).
 *
 * Pre-#663 the same data was inlined in `page.tsx` twice (once per
 * surface), with 5 separate `useState` calls and a hand-written quota
 * row array literal. The config-driven shape keeps the two surfaces in
 * sync structurally — and makes the addon-bearing field set
 * grep-able from a single location.
 *
 * Read-only quotas (e.g. ``max_resource_tokens``) live outside this
 * array because they have no addon to edit. They are rendered in the
 * quota panel separately — see ``READ_ONLY_QUOTA_KEYS`` below.
 */

import type {
  QuotaBreakdown,
  UpdateAddonRequest,
  WorkspaceQuotaDetail,
} from "@/lib/api/admin";

export type AddonKey =
  | "memory"
  | "mcp"
  | "rest"
  | "public"
  | "members"
  | "contexts"
  | "analysis"
  | "storage"
  | "sleep";

export type AddonValuesByKey = Record<AddonKey, number>;

/**
 * Metadata for one quota dimension with addon support.
 *
 * `perUnit` mirrors `ADDON_UNIT_VALUES` in
 * `backend/src/services/addon_calculator_service.py` — the value the
 * input snaps to (HTML `step`) and the count shown in the "+N / unit"
 * hint next to the input label. The backend rejects values not
 * divisible by `perUnit` with HTTP 400, so the client-side snap is
 * a UX affordance rather than the validation gate.
 *
 * `unitSuffix` distinguishes storage (MB) from the count-based
 * addons. For non-storage entries it is the empty string and the
 * "+N / unit" hint reads "+100 / unit". For storage it reads
 * "+100 MB / unit".
 *
 * `proOnly` flags addons that the backend silently clamps for
 * FREE/BASIC tiers (LD-9 in the PUT handler — admin can SET the
 * cache column but `_zero_floor` makes the effective value 0). The
 * dialog renders a small "PRO only" hint next to the input so the
 * admin understands the value will not take effect.
 */
export interface AddonTypeMeta {
  readonly key: AddonKey;
  /** ADDON_UNIT_VALUES count per unit (HTML `step`). */
  readonly perUnit: number;
  /** Display suffix for the perUnit hint (e.g. "MB" for storage). */
  readonly unitSuffix: string;
  /** Plan-tier base quota field on `QuotaBreakdown`. */
  readonly baseField: keyof QuotaBreakdown;
  /** Workspace addon cache field on `WorkspaceQuotaDetail.addon`. */
  readonly addonField: keyof WorkspaceQuotaDetail["addon"];
  /** PUT request body field on `UpdateAddonRequest`. */
  readonly updateField: keyof UpdateAddonRequest;
  /** When present, the live usage counter from `WorkspaceQuotaDetail.usage`. */
  readonly usageField?: keyof WorkspaceQuotaDetail["usage"];
  /** True when the backend `_zero_floor` clamps this addon on FREE/BASIC. */
  readonly proOnly?: boolean;
}

export const ADDON_TYPES: readonly AddonTypeMeta[] = [
  {
    key: "memory",
    perUnit: 10000,
    unitSuffix: "",
    baseField: "memory_limit",
    addonField: "memory_bonus",
    updateField: "addon_memory_bonus",
    usageField: "memories",
  },
  {
    key: "mcp",
    perUnit: 5000,
    unitSuffix: "",
    baseField: "mcp_calls_per_day",
    addonField: "mcp_quota_bonus",
    updateField: "addon_mcp_quota_bonus",
  },
  {
    key: "rest",
    perUnit: 1000,
    unitSuffix: "",
    baseField: "rest_calls_per_day",
    addonField: "rest_quota_bonus",
    updateField: "addon_rest_quota_bonus",
  },
  {
    key: "public",
    perUnit: 500,
    unitSuffix: "",
    baseField: "public_calls_per_day",
    addonField: "public_quota_bonus",
    updateField: "addon_public_quota_bonus",
  },
  {
    key: "members",
    perUnit: 5,
    unitSuffix: "",
    baseField: "max_members",
    addonField: "member_bonus",
    updateField: "addon_member_bonus",
    usageField: "members",
  },
  {
    key: "contexts",
    perUnit: 5,
    unitSuffix: "",
    baseField: "max_contexts",
    addonField: "context_bonus",
    updateField: "addon_context_bonus",
    usageField: "contexts",
  },
  {
    key: "analysis",
    perUnit: 1,
    unitSuffix: "",
    baseField: "analysis_runs_per_day",
    addonField: "analysis_bonus",
    updateField: "addon_analysis_bonus",
  },
  {
    key: "storage",
    perUnit: 100,
    unitSuffix: "MB",
    baseField: "storage_bytes_limit",
    addonField: "storage_bonus_mb",
    updateField: "addon_storage_bonus_mb",
  },
  {
    key: "sleep",
    perUnit: 1,
    unitSuffix: "",
    baseField: "sleep_enabled_contexts_limit",
    addonField: "sleep_contexts_bonus",
    updateField: "addon_sleep_contexts_bonus",
    proOnly: true,
  },
];

/**
 * Quota dimensions surfaced in the panel but NOT editable via addon.
 * Rendered after ``ADDON_TYPES`` in the quota detail panel as read-only
 * rows (base = effective, addon column shows em-dash). Issue #663 adds
 * ``max_resource_tokens`` here — it has tier-fixed values (FREE=0,
 * BASIC=3, PRO=30) and no corresponding addon column on ``Workspace``.
 */
export const READ_ONLY_QUOTA_KEYS = ["maxResourceTokens"] as const;
export type ReadOnlyQuotaKey = (typeof READ_ONLY_QUOTA_KEYS)[number];

export interface ReadOnlyQuotaMeta {
  readonly key: ReadOnlyQuotaKey;
  readonly baseField: keyof QuotaBreakdown;
}

export const READ_ONLY_QUOTAS: readonly ReadOnlyQuotaMeta[] = [
  { key: "maxResourceTokens", baseField: "max_resource_tokens" },
];

/** Initial state for the addon dialog when no quota detail is loaded yet. */
export const EMPTY_ADDON_VALUES: AddonValuesByKey = {
  memory: 0,
  mcp: 0,
  rest: 0,
  public: 0,
  members: 0,
  contexts: 0,
  analysis: 0,
  storage: 0,
  sleep: 0,
};

/**
 * Read the addon cache value from a quota detail response, keyed by
 * `AddonKey`. Centralises the addon-field lookup so callers don't
 * repeat `quotaDetail.addon[meta.addonField]` and don't need to know
 * which field name pairs with which addon key.
 */
export function readAddonFromQuota(
  quotaDetail: WorkspaceQuotaDetail,
  meta: AddonTypeMeta,
): number {
  return quotaDetail.addon[meta.addonField];
}

/**
 * Build a complete addon snapshot from the quota detail response.
 * Used by ``openAddonDialog`` to populate the form state in one step
 * — every key in ``AddonValuesByKey`` is set from the corresponding
 * cache column.
 */
export function snapshotAddonValues(
  quotaDetail: WorkspaceQuotaDetail,
): AddonValuesByKey {
  const snapshot = { ...EMPTY_ADDON_VALUES };
  for (const meta of ADDON_TYPES) {
    snapshot[meta.key] = readAddonFromQuota(quotaDetail, meta);
  }
  return snapshot;
}

/**
 * Build the `UpdateAddonRequest` body from the form state.
 *
 * All 9 fields are sent explicitly. The backend treats omitted fields
 * as "no-touch" (#665 contract) — since the dialog displays and
 * collects every field, sending all 9 each time is correct and
 * matches the legacy 5-field behavior of always sending all values.
 */
export function buildUpdateAddonRequest(
  values: AddonValuesByKey,
): UpdateAddonRequest {
  const body: UpdateAddonRequest = {};
  for (const meta of ADDON_TYPES) {
    body[meta.updateField] = values[meta.key];
  }
  return body;
}

/**
 * Format the in-dialog "effective" preview next to each input.
 *
 * Three cases, in order:
 * 1. `proOnly` addon on a zero-base tier (FREE/BASIC for sleep) → return 0
 *    to mirror the backend `_zero_floor` clamp. The admin sees the same
 *    effective value the backend will report after save, even when they
 *    type a non-zero addon — matching the LD-9 "no effect on this tier"
 *    contract that the inline "(PRO only)" hint promises.
 * 2. Storage → `base` is bytes, `addon` is MB → return MB total so the
 *    preview matches the input unit (which is MB).
 * 3. Default → `base + addon` (same unit, e.g. memory count).
 */
export function computeAddonEffectivePreview(
  meta: AddonTypeMeta,
  base: number,
  addon: number,
): number {
  if (meta.proOnly && base === 0) {
    return 0;
  }
  if (meta.key === "storage") {
    return Math.floor(base / (1024 * 1024)) + addon;
  }
  return base + addon;
}

/**
 * Format a quota row value (base / effective columns of the detail panel).
 *
 * `storage` is rendered as GiB/MiB; everything else uses locale-grouped
 * number formatting. Zero renders as a non-empty placeholder so column
 * width stays stable. Identical formatting rules to the tiers tab
 * `formatStorage` helper for consistency.
 */
export function formatQuotaValue(meta: AddonTypeMeta, value: number): string {
  if (meta.key === "storage") {
    return formatBytes(value);
  }
  return value.toLocaleString();
}

export function formatReadOnlyQuotaValue(
  _meta: ReadOnlyQuotaMeta,
  value: number,
): string {
  return value.toLocaleString();
}

/**
 * Format the addon column of the quota panel.
 *
 * For storage: addon cache value is in MB → "+300 MB".
 * For everything else: "+N" (locale-grouped).
 */
export function formatAddonValue(meta: AddonTypeMeta, value: number): string {
  if (meta.key === "storage") {
    return `+${value.toLocaleString()} MB`;
  }
  return `+${value.toLocaleString()}`;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) {
    return Number.isInteger(gib) ? `${gib} GiB` : `${gib.toFixed(1)} GiB`;
  }
  const mib = bytes / 1024 ** 2;
  if (mib >= 1) {
    return Number.isInteger(mib) ? `${mib} MiB` : `${mib.toFixed(0)} MiB`;
  }
  return `${bytes.toLocaleString()} B`;
}
