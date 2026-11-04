/**
 * adminUserPath (#1861): one place for every admin table that links a user.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { adminUserPath, deleteUser, userDeleteFailure } from "./admin";
import { ApiError, apiClient } from "./base";

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

  it("parses the successor picker the 409 carries (#2420)", () => {
    const error = new ApiError({
      error: "HTTP-409",
      message: "Request failed",
      status: 409,
      details: {
        detail: {
          message: "Ownership transfer required",
          workspace_ids: ["ws-1"],
          context_ids: ["ctx-1"],
          workspaces: [
            {
              id: "ws-1",
              name: "Team",
              owner_user_id: "departing",
              members: [
                { user_id: "u-1", name: "Ann", email: "ann@x.test", role: "admin" },
                // A malformed member is dropped, not fatal.
                { name: "no id" },
              ],
            },
          ],
          contexts: [
            {
              id: "ctx-1",
              name: "notes",
              display_name: "Notes",
              workspace_id: "ws-1",
              is_private: false,
              candidate_user_ids: ["u-1"],
            },
          ],
          owner_fixed_contexts: [
            {
              id: "ctx-2",
              name: "slack",
              display_name: null,
              workspace_id: "ws-2",
              successor_user_id: "owner-2",
            },
          ],
        },
      },
    });
    expect(userDeleteFailure(error)).toEqual({
      kind: "transfer_required",
      workspaces: 1,
      contexts: 1,
      picker: {
        workspaceIds: ["ws-1"],
        workspaces: [
          {
            id: "ws-1",
            name: "Team",
            owner_user_id: "departing",
            members: [
              { user_id: "u-1", name: "Ann", email: "ann@x.test", role: "admin" },
            ],
          },
        ],
        contexts: [
          {
            id: "ctx-1",
            name: "notes",
            display_name: "Notes",
            workspace_id: "ws-1",
            is_private: false,
            candidate_user_ids: ["u-1"],
          },
        ],
        ownerFixedContexts: [
          {
            id: "ctx-2",
            name: "slack",
            display_name: null,
            workspace_id: "ws-2",
            successor_user_id: "owner-2",
          },
        ],
      },
    });
  });

  it("gives an older API's 409 (ids only) the counts and no picker", () => {
    const error = new ApiError({
      error: "HTTP-409",
      message: "Request failed",
      status: 409,
      details: { detail: { workspace_ids: [], context_ids: ["ctx-1"] } },
    });
    const failure = userDeleteFailure(error);
    expect(failure).toEqual({
      kind: "transfer_required",
      workspaces: 0,
      contexts: 1,
    });
    expect(failure).not.toHaveProperty("picker");
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

  it("keeps a server or transport failure generic", () => {
    const server = new ApiError({
      error: "HTTP-500",
      message: "HTTP 500: Internal Server Error",
      status: 500,
    });
    const network = new ApiError({ message: "Failed to fetch", status: 0 });
    expect(userDeleteFailure(server)).toEqual({ kind: "unknown" });
    expect(userDeleteFailure(network)).toEqual({ kind: "unknown" });
  });

  it("knows nothing about a non-API error", () => {
    expect(userDeleteFailure(new Error("boom"))).toEqual({ kind: "unknown" });
  });
});

describe("deleteUser (#2420)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends a bodyless DELETE without selections", async () => {
    const spy = vi.spyOn(apiClient, "delete").mockResolvedValue({});
    await deleteUser("local:admin");
    expect(spy).toHaveBeenCalledWith("/api/v1/admin/users/local%3Aadmin", {});
  });

  it("sends the successors as the JSON body", async () => {
    const spy = vi.spyOn(apiClient, "delete").mockResolvedValue({});
    await deleteUser("u-9", {
      workspace_transfers: { "ws-1": "u-1" },
      context_transfers: {},
    });
    expect(spy).toHaveBeenCalledWith("/api/v1/admin/users/u-9", {
      body: JSON.stringify({
        workspace_transfers: { "ws-1": "u-1" },
        context_transfers: {},
      }),
    });
  });
});
