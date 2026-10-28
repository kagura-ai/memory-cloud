/**
 * adminUserPath (#1861): one place for every admin table that links a user.
 */

import { describe, expect, it } from "vitest";

import { adminUserPath, userDeleteFailure } from "./admin";
import { ApiError } from "./base";

describe("adminUserPath", () => {
  it("points at the admin user page", () => {
    expect(adminUserPath("104714482900000")).toBe(
      "/admin/users/104714482900000",
    );
  });

  it("encodes ids that carry a colon (local / connector identities)", () => {
    expect(adminUserPath("local:admin")).toBe("/admin/users/local%3Aadmin");
  });
});

describe("userDeleteFailure (#2417)", () => {
  it("counts the workspaces and shared contexts a 409 still needs successors for", () => {
    const error = new ApiError({
      error: "HTTP-409",
      message: "Request failed",
      status: 409,
      details: {
        detail: {
          message: "Ownership transfer required",
          workspace_ids: ["ws-1"],
          context_ids: ["ctx-1", "ctx-2"],
        },
      },
    });
    expect(userDeleteFailure(error)).toEqual({
      kind: "transfer_required",
      workspaces: 1,
      contexts: 2,
    });
  });

  it("passes another refusal's message through", () => {
    const error = new ApiError({
      error: "BILLING-005",
      message: "A paid subscription is active for this workspace.",
      status: 409,
      details: { workspace_ids: ["ws-1"] },
    });
    expect(userDeleteFailure(error)).toEqual({
      kind: "message",
      message: "A paid subscription is active for this workspace.",
    });
  });

  it("knows nothing about a non-API error", () => {
    expect(userDeleteFailure(new Error("boom"))).toEqual({ kind: "unknown" });
  });
});
