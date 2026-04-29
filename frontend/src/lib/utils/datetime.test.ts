/**
 * Tests for datetime helpers (Issue #483).
 *
 * Covers:
 *   - formatDateTime renders the correct hour for Asia/Tokyo (JST = UTC+9)
 *     and America/New_York (EDT = UTC-4 in April), proving the timezone
 *     argument is actually applied to Intl.DateTimeFormat.
 *   - formatDate renders the date in the target timezone (the JST date may
 *     differ from the UTC date for late-evening UTC moments).
 *   - formatRelativeTime is locale-aware and timezone-independent by design.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  formatDate,
  formatDateTime,
  formatRelativeTime,
  formatTime,
} from "./datetime";

// 03:56 UTC on 2026-04-29 → 12:56 JST same day, 23:56 EDT prev day.
const SAMPLE_UTC = "2026-04-29T03:56:11Z";

describe("formatDateTime", () => {
  it("renders Asia/Tokyo hour as 12 (UTC+9) in en locale", () => {
    const out = formatDateTime(SAMPLE_UTC, "Asia/Tokyo", "en");
    expect(out).toContain("12:56");
  });

  it("renders Asia/Tokyo hour as 12 (UTC+9) in ja locale", () => {
    const out = formatDateTime(SAMPLE_UTC, "Asia/Tokyo", "ja");
    expect(out).toContain("12:56");
  });

  it("renders America/New_York as 11:56 PM (EDT = UTC-4) on the previous day", () => {
    const out = formatDateTime(SAMPLE_UTC, "America/New_York", "en");
    expect(out).toContain("11:56");
    expect(out).toContain("PM");
    expect(out).toContain("04/28");
  });

  it("renders UTC unchanged when timezone is UTC", () => {
    const out = formatDateTime(SAMPLE_UTC, "UTC", "en");
    expect(out).toContain("03:56");
  });
});

describe("formatDate", () => {
  it("returns the JST date when the UTC moment is late evening UTC", () => {
    // 22:00 UTC on 04-28 → 07:00 JST on 04-29 — different calendar day.
    const out = formatDate("2026-04-28T22:00:00Z", "Asia/Tokyo", "ja");
    expect(out).toContain("2026");
    expect(out).toContain("04/29");
  });

  it("passes through date-only strings without timezone conversion", () => {
    // Backend timeline API returns date-only strings already computed in UTC;
    // converting them through Intl with a tz would shift the calendar date.
    const out = formatDate("2026-04-29", "Asia/Tokyo", "ja");
    expect(out).toContain("2026");
    expect(out).toContain("04/29");
  });
});

describe("formatTime", () => {
  it("renders the time component in the target timezone", () => {
    expect(formatTime(SAMPLE_UTC, "Asia/Tokyo", "ja")).toContain("12:56");
    expect(formatTime(SAMPLE_UTC, "UTC", "ja")).toContain("03:56");
  });
});

describe("formatRelativeTime", () => {
  // Freeze the clock so date-fns rounding boundaries (e.g. 5→4 minutes) cannot
  // tip under slow CI or clock skew.
  const NOW = new Date("2026-04-29T12:00:00Z");
  const FIVE_MIN_AGO = new Date(NOW.getTime() - 5 * 60 * 1000).toISOString();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns an English relative string for en locale", () => {
    expect(formatRelativeTime(FIVE_MIN_AGO, "en")).toMatch(/minute/);
  });

  it("returns a Japanese relative string for ja locale", () => {
    expect(formatRelativeTime(FIVE_MIN_AGO, "ja")).toMatch(/分/);
  });

  it("omits the suffix when addSuffix=false", () => {
    const withSuffix = formatRelativeTime(FIVE_MIN_AGO, "en", true);
    const withoutSuffix = formatRelativeTime(FIVE_MIN_AGO, "en", false);
    expect(withSuffix).toMatch(/ago|in /);
    expect(withoutSuffix).not.toMatch(/ago|in /);
  });
});
