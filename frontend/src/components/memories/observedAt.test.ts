import { describe, expect, it } from "vitest";

import { formatDateTime } from "@/lib/utils/datetime";
import { formatObservedAt } from "./observedAt";

describe("formatObservedAt (#2330)", () => {
  it("keeps a date-only value as the calendar day in every timezone", () => {
    for (const tz of ["UTC", "Asia/Tokyo", "America/Los_Angeles"]) {
      expect(formatObservedAt("2024-03-07", tz, "en")).toBe("2024-03-07");
    }
  });

  it("formats a UTC datetime like the other timestamps", () => {
    const value = "2024-03-07T23:30:00Z";
    expect(formatObservedAt(value, "Asia/Tokyo", "ja")).toBe(
      formatDateTime(value, "Asia/Tokyo", "ja"),
    );
  });
});
