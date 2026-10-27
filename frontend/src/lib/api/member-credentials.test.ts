/**
 * Error text of the member-credentials wrappers (#2202): every wrapper shows
 * the API's `detail` (HTTPException) or `message` (the app's `{error,
 * message}` envelope), and falls back to "<action>: <status text>".
 */

import { describe, it, expect, afterEach, vi } from "vitest";

import {
  createAPIKey,
  deleteWorkspaceMemberAPIKeyById,
  getMemberCredentials,
  regenerateOAuthSecret,
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

  it("prefers detail over message", async () => {
    respond(400, "Bad Request", { detail: "from detail", message: "from message" });
    await expect(regenerateOAuthSecret("ws", "u")).rejects.toThrow(
      "from detail",
    );
  });

  it("falls back to the status text for a non-JSON body", async () => {
    respond(502, "Bad Gateway");
    await expect(regenerateOAuthSecret("ws", "u")).rejects.toThrow(
      "Failed to regenerate OAuth secret: Bad Gateway",
    );
  });

  it("ignores a non-string detail (validation error list)", async () => {
    respond(422, "Unprocessable Entity", {
      detail: [{ loc: ["body", "name"], msg: "field required" }],
    });
    await expect(createAPIKey("ws", "u", { name: "" })).rejects.toThrow(
      "Failed to create API key: Unprocessable Entity",
    );
  });
});
