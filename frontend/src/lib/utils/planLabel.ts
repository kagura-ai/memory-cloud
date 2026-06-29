/**
 * Plan-tier display-label resolution.
 *
 * Issue #350 established customizable plan display names for SaaS forks. The
 * OSS default stays **S / M / L** (neutral, locale-independent) so the public
 * Apache-2.0 repo carries no operator-specific branding. A deployment (e.g.
 * Kagura's own site) overrides the labels — optionally per-locale — entirely
 * through `NEXT_PUBLIC_*` env, so its commercial labels never live in the repo.
 *
 * Resolution order (first hit wins):
 *   1. Locale-aware JSON map  — NEXT_PUBLIC_PLAN_DISPLAY_NAMES
 *      e.g. {"en":{"free":"Trial","basic":"Starter","pro":"Pro"},
 *            "ja":{"free":"お試し","basic":"スターター","pro":"プロ"}}
 *      (exact locale, then its base language: "ja-JP" → "ja")
 *   2. Single-string env (locale-blind, back-compat with #350)
 *      — NEXT_PUBLIC_PLAN_{FREE,BASIC,PRO}_DISPLAY_NAME
 *   3. OSS default — S / M / L
 */

export type PlanTier = "free" | "basic" | "pro";

/** OSS default labels — intentionally neutral. Do not localize these. */
export const DEFAULT_PLAN_LABELS: Record<PlanTier, string> = {
  free: "S",
  basic: "M",
  pro: "L",
};

export type LocalePlanLabelMap = Record<
  string,
  Partial<Record<PlanTier, string>>
>;

/**
 * Parse the NEXT_PUBLIC_PLAN_DISPLAY_NAMES JSON env value into a locale→tier
 * label map. Tolerant by design: any malformed / non-object / array input
 * yields an empty map so resolution falls back to S/M/L rather than throwing.
 */
export function parsePlanDisplayNames(
  raw: string | null | undefined,
): LocalePlanLabelMap {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as LocalePlanLabelMap;
    }
    return {};
  } catch {
    return {};
  }
}

/**
 * Pure label resolver. Sources are injected so this is trivially testable
 * without touching the environment.
 */
export function resolvePlanLabel(
  planName: PlanTier,
  locale: string | undefined,
  jsonMap: LocalePlanLabelMap = {},
  singleMap: Partial<Record<PlanTier, string>> = {},
): string {
  if (locale) {
    const exact = jsonMap[locale]?.[planName];
    if (exact) return exact;
    const base = locale.split("-")[0];
    if (base && base !== locale) {
      const byBase = jsonMap[base]?.[planName];
      if (byBase) return byBase;
    }
  }

  const single = singleMap[planName];
  if (single) return single;

  return DEFAULT_PLAN_LABELS[planName];
}

/**
 * Env-wired convenience used by UI components. The `NEXT_PUBLIC_*` references
 * are static member expressions so Next.js inlines them at build time.
 */
export function planLabelFromEnv(
  planName: PlanTier,
  locale: string | undefined,
): string {
  return resolvePlanLabel(
    planName,
    locale,
    parsePlanDisplayNames(process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES),
    {
      free: process.env.NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME,
      basic: process.env.NEXT_PUBLIC_PLAN_BASIC_DISPLAY_NAME,
      pro: process.env.NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME,
    },
  );
}
