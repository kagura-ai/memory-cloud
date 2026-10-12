import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/base";

import { createKeyErrorText } from "./CreateAPIKeyDialog";

describe("createKeyErrorText", () => {
  it("renders a 422 field message instead of the raw array (#1944)", () => {
    const err = new ApiError({
      error: "VAL-001",
      message: "Validation error",
      status: 422,
      details: {
        detail: [
          {
            loc: ["body", "name"],
            msg: "Value error, names starting with 'connector:' are reserved for connector keys",
            type: "value_error",
          },
        ],
      },
    });
    expect(createKeyErrorText(err)).toContain("reserved for connector keys");
  });

  it("keeps a plain string detail and falls back to the message", () => {
    expect(
      createKeyErrorText(new ApiError({ message: "m", status: 400, details: { detail: "taken" } })),
    ).toBe("taken");
    expect(createKeyErrorText(new ApiError({ message: "boom", status: 500 }))).toBe("boom");
    expect(createKeyErrorText(new Error("plain"))).toBe("plain");
    expect(createKeyErrorText("x")).toBe("Failed to create API key");
  });
});
