/**
 * Error text of the member-credentials wrappers (#2202): every wrapper shows
 * the API's `message` (the app's `{error, message, details}` envelope) or a
 * plain `detail` string, and falls back to "<action>: HTTP <status>".
 */

import { describe, it, expect, afterEach, vi } from "vitest";

import {
  createAPIKey,
  deleteWorkspaceMemberAPIKeyById,
  getMemberCredentials,
  regenerateOAuthSecret,
  regenerateAPIKey,
} from "./member-credentials";

function respond(status: number, statusText: string, body?: unknown) {
  const response = {
    ok: false,
    status,
    statusText,
    json: () =>
      body === undefined
        ? Promise.reject(new SyntaxError("Unexpected end of JSON input"))
        : Promise.resolve(body),
  } as unknown as Response;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("member-credentials error text", () => {
  it("shows the envelope message", async () => {
    respond(403, "Forbidden", {
      error: "forbidden",
      message: "Only the workspace owner can delete this key",
    });
    await expect(
      deleteWorkspaceMemberAPIKeyById("ws", "u", "akey_1"),
    ).rejects.toThrow("Only the workspace owner can delete this key");
  });

  it("shows an HTTPException detail", async () => {
    respond(404, "Not Found", { detail: "Member not found" });
    await expect(getMemberCredentials("ws", "u")).rejects.toThrow(
      "Member not found",
    );
  });

  it("prefers message over detail, like ApiClient", async () => {
    respond(400, "Bad Request", { detail: "from detail", message: "from message" });
    await expect(regenerateOAuthSecret("ws", "u")).rejects.toThrow(
      "from message",
    );
  });

  it("shows the 422 envelope message", async () => {
    respond(422, "Unprocessable Entity", {
      error: "VAL-001",
      message: "Request validation failed",
      details: { errors: [{ loc: ["body", "name"], msg: "field required" }] },
    });
    await expect(createAPIKey("ws", "u", { name: "" })).rejects.toThrow(
      "Request validation failed",
    );
  });

  it("falls back to the HTTP status for a non-JSON body", async () => {
    respond(502, "Bad Gateway");
    await expect(regenerateOAuthSecret("ws", "u")).rejects.toThrow(
      "Failed to regenerate OAuth secret: HTTP 502 Bad Gateway",
    );
  });

  it("falls back when the status text is empty (HTTP/2)", async () => {
    respond(502, "");
    await expect(getMemberCredentials("ws", "u")).rejects.toThrow(
      /^Failed to get credentials: HTTP 502$/,
    );
  });

  it("ignores a non-string detail", async () => {
    respond(422, "Unprocessable Entity", {
      detail: [{ loc: ["body", "name"], msg: "field required" }],
    });
    await expect(createAPIKey("ws", "u", { name: "" })).rejects.toThrow(
      "Failed to create API key: HTTP 422 Unprocessable Entity",
    );
  });
});


describe("selected-key regeneration (#2379)", () => {
  it("includes the selected public id in the authenticated request", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ key: "replacement" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const keyId = "akey_0000000000000000000002";
    await regenerateAPIKey("ws-1", "user-1", keyId);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(`/api-key/regenerate?key_id=${keyId}`),
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });
});
