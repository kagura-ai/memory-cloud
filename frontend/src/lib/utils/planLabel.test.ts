import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_PLAN_LABELS,
  parsePlanDisplayNames,
  planLabelFromEnv,
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
      en: { free: "Trial", basic: "Starter", pro: "Pro" },
      ja: { free: "お試し", basic: "スターター", pro: "プロ" },
    });
    expect(parsePlanDisplayNames(raw)).toEqual({
      en: { free: "Trial", basic: "Starter", pro: "Pro" },
      ja: { free: "お試し", basic: "スターター", pro: "プロ" },
    });
  });
});

describe("resolvePlanLabel", () => {
  it("falls back to the OSS S/M/L default when nothing is configured", () => {
    expect(resolvePlanLabel("free", "ja")).toBe("S");
    expect(resolvePlanLabel("basic", "en")).toBe("M");
    expect(resolvePlanLabel("pro", undefined)).toBe("L");
    expect(resolvePlanLabel("free", "en")).toBe(DEFAULT_PLAN_LABELS.free);
  });

  it("uses the locale-aware JSON map when present", () => {
    const map: LocalePlanLabelMap = {
      en: { free: "Trial", basic: "Starter", pro: "Pro" },
      ja: { free: "お試し", basic: "スターター", pro: "プロ" },
    };
    expect(resolvePlanLabel("free", "ja", map)).toBe("お試し");
    expect(resolvePlanLabel("basic", "ja", map)).toBe("スターター");
    expect(resolvePlanLabel("pro", "en", map)).toBe("Pro");
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
  ];

  afterEach(() => {
    for (const k of ENV_KEYS) delete process.env[k];
  });

  it("returns S/M/L when no env is set", () => {
    expect(planLabelFromEnv("free", "ja")).toBe("S");
    expect(planLabelFromEnv("pro", "en")).toBe("L");
  });

  it("reads the locale-aware JSON env", () => {
    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      en: { free: "Trial", basic: "Starter", pro: "Pro" },
      ja: { free: "お試し", basic: "スターター", pro: "プロ" },
    });
    expect(planLabelFromEnv("free", "ja")).toBe("お試し");
    expect(planLabelFromEnv("basic", "en")).toBe("Starter");
  });

  it("reads the single-string env as a back-compat fallback", () => {
    process.env.NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME = "Enterprise";
    expect(planLabelFromEnv("pro", "ja")).toBe("Enterprise");
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
