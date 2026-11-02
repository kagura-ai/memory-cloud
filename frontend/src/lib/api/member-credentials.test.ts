/**
 * The member-credentials wrappers go through `ApiClient` (#2384): a refusal
 * reaches the caller as an `ApiError` carrying the error code, the details and
 * the normalised plan-gate facts, so the UI renders it like every other API
 * error. The error text keeps the precedence #2202 pinned: the envelope's
 * `message`, then a plain `detail` string, then the HTTP status.
 */

import { describe, it, expect, afterEach, vi } from "vitest";

import { ApiError } from "./base";
import {
  createAPIKey,
  deleteOAuthApp,
  deleteWorkspaceMemberAPIKey,
  deleteWorkspaceMemberAPIKeyById,
  getMemberCredentials,
  hideAPIKey,
  hideOAuthClientSecret,
  regenerateOAuthSecret,
  regenerateAPIKey,
} from "./member-credentials";

const JSON_HEADERS = { "content-type": "application/json" };

/** Stub fetch with one response; returns the mock for call assertions. */
function respond(status: number, statusText: string, body?: unknown) {
  const response =
    body === undefined
      ? new Response(null, { status, statusText })
      : new Response(JSON.stringify(body), {
          status,
          statusText,
          headers: JSON_HEADERS,
        });
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The rejection a call ends in, for field-level assertions. */
async function rejectionOf(call: Promise<unknown>): Promise<unknown> {
  try {
    await call;
  } catch (err) {
    return err;
  }
  throw new Error("expected the call to reject");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("member-credentials refusals reach the caller as ApiError", () => {
  it("carries the code and the plan-gate facts of a plan refusal on key creation", async () => {
    respond(403, "Forbidden", {
      error: "FEAT-001",
      message: "Feature 'public_contexts' is not available on your plan",
      details: {
        gate: "plan",
        feature: "public_contexts",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "free",
      },
    });

    const err = await rejectionOf(
      createAPIKey("ws", "u", { name: "pub", bound_context_id: "ctx-1" }),
    );

    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(403);
    expect(apiErr.error).toBe("FEAT-001");
    expect(apiErr.message).toBe(
      "Feature 'public_contexts' is not available on your plan",
    );
    expect(apiErr.gate).toEqual({
      state: "plan",
      feature: "public_contexts",
      requiredPlan: "pro",
      requiredPlanLabel: "L",
      currentPlan: "free",
    });
  });

  it("carries a quota refusal's counts", async () => {
    respond(429, "Too Many Requests", {
      error: "QUOTA-001",
      message: "Context limit reached",
      details: { gate: "quota", quota_type: "contexts", current: 3, limit: 3 },
    });

    const err = (await rejectionOf(getMemberCredentials("ws", "u"))) as ApiError;

    expect(err).toBeInstanceOf(ApiError);
    expect(err.gate).toMatchObject({
      state: "quota",
      quotaType: "contexts",
      current: 3,
      limit: 3,
    });
  });

  it("has no gate on an ordinary refusal", async () => {
    respond(404, "Not Found", { detail: "Member not found" });
    const err = (await rejectionOf(getMemberCredentials("ws", "u"))) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.gate).toBeUndefined();
  });

  it.each([
    ["hideAPIKey", () => hideAPIKey("ws", "u")],
    ["regenerateAPIKey", () => regenerateAPIKey("ws", "u", "akey_1")],
    [
      "deleteWorkspaceMemberAPIKeyById",
      () => deleteWorkspaceMemberAPIKeyById("ws", "u", "akey_1"),
    ],
    [
      "deleteWorkspaceMemberAPIKey",
      () => deleteWorkspaceMemberAPIKey("ws", "u", "akey_1"),
    ],
    ["hideOAuthClientSecret", () => hideOAuthClientSecret("client-1")],
    ["regenerateOAuthSecret", () => regenerateOAuthSecret("ws", "u")],
    ["deleteOAuthApp", () => deleteOAuthApp("ws", "u")],
  ])("%s rejects with an ApiError", async (_name, call) => {
    respond(403, "Forbidden", { error: "AUTH-101", message: "Not allowed" });
    const err = await rejectionOf(call());
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).error).toBe("AUTH-101");
  });
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

  it("prefers message over detail", async () => {
    respond(400, "Bad Request", {
      detail: "from detail",
      message: "from message",
    });
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

  it("falls back to the HTTP status for an empty body", async () => {
    respond(502, "Bad Gateway");
    await expect(regenerateOAuthSecret("ws", "u")).rejects.toThrow(
      "HTTP 502: Bad Gateway",
    );
  });
});

describe("member-credentials success paths", () => {
  const BASE = /^https?:\/\/[^/]+/;

  function calledWith(fetchMock: ReturnType<typeof vi.fn>) {
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    return { path: url.replace(BASE, ""), init };
  }

  it("getMemberCredentials returns the payload", async () => {
    const payload = { api_keys: [], target_user_role: "owner" };
    const fetchMock = respond(200, "OK", payload);
    await expect(getMemberCredentials("ws-1", "user-1")).resolves.toEqual(
      payload,
    );
    const { path, init } = calledWith(fetchMock);
    expect(path).toBe("/api/v1/workspaces/ws-1/members/user-1/credentials");
    expect(init).toMatchObject({ method: "GET", credentials: "include" });
  });

  it("createAPIKey posts the body and returns the new key", async () => {
    const key = { id: "akey_1", name: "ci" };
    const fetchMock = respond(201, "Created", key);
    const data = { name: "ci", default_context_id: "ctx-1" };
    await expect(createAPIKey("ws-1", "user-1", data)).resolves.toEqual(key);
    const { path, init } = calledWith(fetchMock);
    expect(path).toBe(
      "/api/v1/workspaces/ws-1/members/user-1/credentials/api-keys",
    );
    expect(init).toMatchObject({ method: "POST", credentials: "include" });
    expect(JSON.parse(init.body as string)).toEqual(data);
  });

  it("regenerateAPIKey sends the selected public id (#2379) and returns the key", async () => {
    const result = { key: "replacement", key_prefix: "kag_", key_id: "akey_2" };
    const fetchMock = respond(200, "OK", result);
    const keyId = "akey_0000000000000000000002";
    await expect(regenerateAPIKey("ws-1", "user-1", keyId)).resolves.toEqual(
      result,
    );
    const { path, init } = calledWith(fetchMock);
    expect(path).toBe(
      `/api/v1/workspaces/ws-1/members/user-1/credentials/api-key/regenerate?key_id=${keyId}`,
    );
    expect(init).toMatchObject({ method: "POST", credentials: "include" });
  });

  it("regenerateOAuthSecret returns the new secret", async () => {
    const result = { client_secret: "s", client_id: "c" };
    const fetchMock = respond(200, "OK", result);
    await expect(regenerateOAuthSecret("ws-1", "user-1")).resolves.toEqual(
      result,
    );
    expect(calledWith(fetchMock).path).toBe(
      "/api/v1/workspaces/ws-1/members/user-1/credentials/oauth/regenerate",
    );
  });

  it.each([
    [
      "hideAPIKey",
      () => hideAPIKey("ws-1", "user-1"),
      "POST",
      "/api/v1/workspaces/ws-1/members/user-1/credentials/api-key/hide",
    ],
    [
      "deleteWorkspaceMemberAPIKeyById",
      () => deleteWorkspaceMemberAPIKeyById("ws-1", "user-1", "akey_1"),
      "DELETE",
      "/api/v1/workspaces/ws-1/members/user-1/credentials/api-keys/akey_1",
    ],
    [
      "deleteWorkspaceMemberAPIKey",
      () => deleteWorkspaceMemberAPIKey("ws-1", "user-1", "akey_1"),
      "DELETE",
      "/api/v1/workspaces/ws-1/members/user-1/credentials/api-keys/akey_1",
    ],
    [
      "hideOAuthClientSecret",
      () => hideOAuthClientSecret("client-1"),
      "POST",
      "/api/v1/oauth/clients/client-1/hide",
    ],
    [
      "deleteOAuthApp",
      () => deleteOAuthApp("ws-1", "user-1"),
      "DELETE",
      "/api/v1/workspaces/ws-1/members/user-1/credentials/oauth",
    ],
  ])("%s resolves on 204", async (_name, call, method, expectedPath) => {
    const fetchMock = respond(204, "No Content");
    await expect(call()).resolves.toBeUndefined();
    const { path, init } = calledWith(fetchMock);
    expect(path).toBe(expectedPath);
    expect(init).toMatchObject({ method, credentials: "include" });
  });
});
