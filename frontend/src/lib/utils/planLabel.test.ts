import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_PLAN_LABELS,
  PLAN_TIER_ORDER,
  isPaidTier,
  isPlanTier,
  parsePlanDisplayNames,
  planAtLeast,
  planLabelFromEnv,
  planRank,
  resolvePlanLabel,
  type LocalePlanLabelMap,
} from "./planLabel";

describe("parsePlanDisplayNames", () => {
  it("returns an empty map for empty / null / undefined input", () => {
    expect(parsePlanDisplayNames(undefined)).toEqual({});
    expect(parsePlanDisplayNames(null)).toEqual({});
    expect(parsePlanDisplayNames("")).toEqual({});
  });

  it("returns an empty map for malformed JSON instead of throwing", () => {
    expect(parsePlanDisplayNames("{not json")).toEqual({});
  });

  it("returns an empty map for non-object JSON (array / scalar)", () => {
    expect(parsePlanDisplayNames("[1,2,3]")).toEqual({});
    expect(parsePlanDisplayNames("42")).toEqual({});
    expect(parsePlanDisplayNames('"hi"')).toEqual({});
  });

  it("parses a valid locale→tier map", () => {
    const raw = JSON.stringify({
      en: { free: "Trial", basic: "Starter", pro: "Pro", promax: "Pro Max" },
      ja: {
        free: "お試し",
        basic: "スターター",
        pro: "プロ",
        promax: "プロマックス",
      },
    });
    expect(parsePlanDisplayNames(raw)).toEqual({
      en: { free: "Trial", basic: "Starter", pro: "Pro", promax: "Pro Max" },
      ja: {
        free: "お試し",
        basic: "スターター",
        pro: "プロ",
        promax: "プロマックス",
      },
    });
  });
});

describe("resolvePlanLabel", () => {
  it("falls back to the OSS S/M/L/XL default when nothing is configured", () => {
    expect(resolvePlanLabel("free", "ja")).toBe("S");
    expect(resolvePlanLabel("basic", "en")).toBe("M");
    expect(resolvePlanLabel("pro", undefined)).toBe("L");
    expect(resolvePlanLabel("promax", "en")).toBe("XL");
    expect(resolvePlanLabel("free", "en")).toBe(DEFAULT_PLAN_LABELS.free);
  });

  it("uses the locale-aware JSON map when present", () => {
    const map: LocalePlanLabelMap = {
      en: { free: "Trial", basic: "Starter", pro: "Pro", promax: "Pro Max" },
      ja: {
        free: "お試し",
        basic: "スターター",
        pro: "プロ",
        promax: "プロマックス",
      },
    };
    expect(resolvePlanLabel("free", "ja", map)).toBe("お試し");
    expect(resolvePlanLabel("basic", "ja", map)).toBe("スターター");
    expect(resolvePlanLabel("pro", "en", map)).toBe("Pro");
    expect(resolvePlanLabel("promax", "ja", map)).toBe("プロマックス");
  });

  it("falls back from a regional locale to its base language (ja-JP → ja)", () => {
    const map: LocalePlanLabelMap = { ja: { free: "お試し" } };
    expect(resolvePlanLabel("free", "ja-JP", map)).toBe("お試し");
  });

  it("prefers the JSON map over the single-string env map", () => {
    const json: LocalePlanLabelMap = { ja: { pro: "プロ" } };
    const single = { pro: "L-CUSTOM" };
    expect(resolvePlanLabel("pro", "ja", json, single)).toBe("プロ");
  });

  it("falls back to the single-string map when the JSON map lacks the locale/tier", () => {
    const json: LocalePlanLabelMap = { ja: { free: "お試し" } };
    const single = { pro: "L-CUSTOM" };
    // 'pro' is absent from the ja map → single-string override wins over default
    expect(resolvePlanLabel("pro", "ja", json, single)).toBe("L-CUSTOM");
  });

  it("falls back to default when neither map has the tier", () => {
    const json: LocalePlanLabelMap = { ja: { free: "お試し" } };
    expect(resolvePlanLabel("basic", "ja", json, {})).toBe("M");
  });
});

describe("planLabelFromEnv", () => {
  const ENV_KEYS = [
    "NEXT_PUBLIC_PLAN_DISPLAY_NAMES",
    "NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME",
    "NEXT_PUBLIC_PLAN_BASIC_DISPLAY_NAME",
    "NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME",
    "NEXT_PUBLIC_PLAN_PROMAX_DISPLAY_NAME",
  ];

  afterEach(() => {
    for (const k of ENV_KEYS) delete process.env[k];
  });

  it("returns S/M/L/XL when no env is set", () => {
    expect(planLabelFromEnv("free", "ja")).toBe("S");
    expect(planLabelFromEnv("pro", "en")).toBe("L");
    expect(planLabelFromEnv("promax", "en")).toBe("XL");
  });

  it("reads the locale-aware JSON env", () => {
    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      en: { free: "Trial", basic: "Starter", pro: "Pro", promax: "Pro Max" },
      ja: {
        free: "お試し",
        basic: "スターター",
        pro: "プロ",
        promax: "プロマックス",
      },
    });
    expect(planLabelFromEnv("free", "ja")).toBe("お試し");
    expect(planLabelFromEnv("basic", "en")).toBe("Starter");
    expect(planLabelFromEnv("promax", "ja")).toBe("プロマックス");
  });

  it("reads the single-string env as a back-compat fallback", () => {
    process.env.NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME = "Enterprise";
    expect(planLabelFromEnv("pro", "ja")).toBe("Enterprise");
    process.env.NEXT_PUBLIC_PLAN_PROMAX_DISPLAY_NAME = "Pro Max";
    expect(planLabelFromEnv("promax", "ja")).toBe("Pro Max");
  });

  it("re-parses when the JSON env value changes (cache invalidation)", () => {
    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      ja: { pro: "プロ" },
    });
    expect(planLabelFromEnv("pro", "ja")).toBe("プロ");

    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      ja: { pro: "PRO-2" },
    });
    expect(planLabelFromEnv("pro", "ja")).toBe("PRO-2");
  });
});

// #1548: tier ordering + comparison helpers (mirror backend PLAN_ORDER).
describe("PLAN_TIER_ORDER / isPlanTier", () => {
  it("lists the four tiers lowest → highest and covers every default label", () => {
    expect(PLAN_TIER_ORDER).toEqual(["free", "basic", "pro", "promax"]);
    expect(Object.keys(DEFAULT_PLAN_LABELS).sort()).toEqual(
      [...PLAN_TIER_ORDER].sort(),
    );
  });

  it("isPlanTier accepts only the four canonical tier names", () => {
    for (const tier of PLAN_TIER_ORDER) expect(isPlanTier(tier)).toBe(true);
    expect(isPlanTier("enterprise")).toBe(false);
    expect(isPlanTier("PRO")).toBe(false);
    expect(isPlanTier("")).toBe(false);
    expect(isPlanTier(null)).toBe(false);
    expect(isPlanTier(undefined)).toBe(false);
    expect(isPlanTier(2)).toBe(false);
  });
});

describe("planRank / planAtLeast / isPaidTier", () => {
  it("ranks tiers by PLAN_TIER_ORDER index", () => {
    expect(planRank("free")).toBe(0);
    expect(planRank("basic")).toBe(1);
    expect(planRank("pro")).toBe(2);
    expect(planRank("promax")).toBe(3);
  });

  it("ranks unknown / null / undefined as 0 (fail closed, like the backend)", () => {
    expect(planRank("enterprise")).toBe(0);
    expect(planRank(null)).toBe(0);
    expect(planRank(undefined)).toBe(0);
    expect(planRank("")).toBe(0);
  });

  it("planAtLeast treats the minimum tier and every higher tier as satisfying", () => {
    expect(planAtLeast("promax", "pro")).toBe(true);
    expect(planAtLeast("pro", "pro")).toBe(true);
    expect(planAtLeast("basic", "pro")).toBe(false);
    expect(planAtLeast("free", "pro")).toBe(false);
    expect(planAtLeast("promax", "promax")).toBe(true);
    expect(planAtLeast("pro", "promax")).toBe(false);
    expect(planAtLeast("basic", "basic")).toBe(true);
  });

  it("planAtLeast is false for unknown / null plans against any paid minimum", () => {
    expect(planAtLeast("enterprise", "basic")).toBe(false);
    expect(planAtLeast(null, "basic")).toBe(false);
    expect(planAtLeast(undefined, "pro")).toBe(false);
    // …but trivially true against "free" (rank 0 ≥ 0) — same as the backend.
    expect(planAtLeast(undefined, "free")).toBe(true);
  });

  it("isPaidTier is true for every tier above free and false otherwise", () => {
    expect(isPaidTier("basic")).toBe(true);
    expect(isPaidTier("pro")).toBe(true);
    expect(isPaidTier("promax")).toBe(true);
    expect(isPaidTier("free")).toBe(false);
    expect(isPaidTier("enterprise")).toBe(false);
    expect(isPaidTier(null)).toBe(false);
    expect(isPaidTier(undefined)).toBe(false);
  });
});
