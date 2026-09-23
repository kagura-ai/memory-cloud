import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./base";

describe("ApiError", () => {
  it("is an instance of Error", () => {
    const err = new ApiError({ message: "Not found", status: 404 });
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(ApiError);
  });

  it("sets .message from constructor", () => {
    const err = new ApiError({ message: "Something went wrong", status: 500 });
    expect(err.message).toBe("Something went wrong");
  });

  it("sets .name to ApiError", () => {
    const err = new ApiError({ message: "test", status: 400 });
    expect(err.name).toBe("ApiError");
  });

  it("exposes .status, .error, and .details", () => {
    const err = new ApiError({
      error: "RES-001",
      message: "Resource limit exceeded",
      status: 429,
      details: { limit: 100 },
    });
    expect(err.status).toBe(429);
    expect(err.error).toBe("RES-001");
    expect(err.details).toEqual({ limit: 100 });
  });

  it("has a .stack trace", () => {
    const err = new ApiError({ message: "test", status: 500 });
    expect(err.stack).toBeDefined();
    expect(err.stack).toContain("ApiError");
  });

  it("works with instanceof Error in catch blocks", () => {
    let caught: unknown;
    try {
      throw new ApiError({ message: "backend error", status: 422 });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe("backend error");
  });

  it("defaults .error and .details to undefined when omitted", () => {
    const err = new ApiError({ message: "minimal", status: 0 });
    expect(err.error).toBeUndefined();
    expect(err.details).toBeUndefined();
  });
});

describe("ApiClient error normalization (#992 canonical 422 envelope)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function mockFetchOnce(status: number, body: unknown) {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  }

  it("aliases canonical 422 details.errors -> details.detail so field consumers keep working", async () => {
    const errors = [
      { loc: ["body", "redirect_uris"], msg: "Field required", type: "missing" },
    ];
    mockFetchOnce(422, {
      error: "VAL-001",
      message: "Request validation failed",
      details: { errors },
    });

    const client = new ApiClient("http://test");
    const caught = await client.post("/x", {}).catch((e: unknown) => e);

    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(422);
    expect(err.error).toBe("VAL-001");
    const details = err.details as Record<string, unknown>;
    // Existing field-validation consumers read details.detail as the array.
    expect(details.detail).toEqual(errors);
    expect(details.errors).toEqual(errors);
  });

  it("leaves a string `detail` from a raw HTTPException endpoint untouched", async () => {
    // Not-yet-converted endpoints still emit FastAPI's { detail: "..." }.
    mockFetchOnce(404, { detail: "API key not found" });

    const client = new ApiClient("http://test");
    const caught = await client.get("/x").catch((e: unknown) => e);

    expect(caught).toBeInstanceOf(ApiError);
    const details = (caught as ApiError).details as Record<string, unknown>;
    expect(details.detail).toBe("API key not found");
  });

  it("aliases the reshaped HTTP-<status> message back to details.detail (#992 Phase 2)", async () => {
    // The global StarletteHTTPException handler reshapes raw {detail} errors
    // into { error: "HTTP-404", message, details: {} }. Consumers reading
    // details.detail must still get the human message string.
    mockFetchOnce(404, {
      error: "HTTP-404",
      message: "Resource not found",
      details: {},
    });

    const client = new ApiClient("http://test");
    const caught = await client.get("/x").catch((e: unknown) => e);

    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.error).toBe("HTTP-404");
    expect(err.message).toBe("Resource not found");
    const details = err.details as Record<string, unknown>;
    // String (not array) so consumers calling details.detail.includes(...) work.
    expect(details.detail).toBe("Resource not found");
  });

  it("preserves a reshaped dict detail under details.detail (#992 Phase 2)", async () => {
    // Dict-detail HTTPExceptions (external-keys conflicts) are reshaped to
    // { error: "HTTP-409", message: "Request failed", details: { detail: {...} } }.
    // Consumers branching on details.detail.error must still see the object —
    // the HTTP-* message alias must NOT overwrite it.
    const payload = { error: "reranker_provider_conflict", conflicting_provider: "cohere" };
    mockFetchOnce(409, {
      error: "HTTP-409",
      message: "Request failed",
      details: { detail: payload },
    });

    const client = new ApiClient("http://test");
    const caught = await client.post("/x", {}).catch((e: unknown) => e);

    expect(caught).toBeInstanceOf(ApiError);
    const details = (caught as ApiError).details as Record<string, unknown>;
    expect(details.detail).toEqual(payload);
  });

  it("does not inject a synthetic detail into a canonical (non-HTTP) error body", async () => {
    // The message->detail alias is scoped to the reshaped HTTP-* placeholder
    // code, so a semantic MemoryCloudException body keeps its authoritative
    // details untouched (no fabricated `detail` key).
    mockFetchOnce(429, {
      error: "RES-001",
      message: "Resource limit exceeded",
      details: { limit: 100 },
    });

    const client = new ApiClient("http://test");
    const caught = await client.get("/x").catch((e: unknown) => e);

    const details = (caught as ApiError).details as Record<string, unknown>;
    expect(details).toEqual({ limit: 100 });
    expect(details.detail).toBeUndefined();
  });
});

describe("ApiClient gate normalization (#1644)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function mockFetchOnce(status: number, body: unknown) {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  }

  async function caught(): Promise<ApiError> {
    const client = new ApiClient("http://test");
    const err = await client.post("/x", {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    return err as ApiError;
  }

  it("attaches err.gate for a FEAT-001 plan refusal", async () => {
    mockFetchOnce(403, {
      error: "FEAT-001",
      message: "Feature 'team_invitations' not available on M plan.",
      details: {
        gate: "plan",
        feature: "team_invitations",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "basic",
      },
    });

    const err = await caught();
    expect(err.gate).toEqual({
      state: "plan",
      feature: "team_invitations",
      requiredPlan: "pro",
      requiredPlanLabel: "L",
      currentPlan: "basic",
    });
    // The raw details are untouched: gate is additive.
    expect(err.details?.required_plan).toBe("pro");
  });

  it("attaches err.gate for a QUOTA-001 refusal from a server predating #1644", async () => {
    mockFetchOnce(429, {
      error: "QUOTA-001",
      message: "Workspace limit reached.",
      details: {
        quota_type: "workspace_limit_reached",
        owned_count: 2,
        cap: 2,
      },
    });

    const err = await caught();
    expect(err.gate).toEqual({
      state: "quota",
      quotaType: "workspace_limit_reached",
      current: 2,
      limit: 2,
    });
  });

  it("attaches a role gate for an AUTH-101 403 whose details were stripped", async () => {
    mockFetchOnce(403, {
      error: "AUTH-101",
      message: "Insufficient permissions",
      details: {},
    });

    expect((await caught()).gate).toEqual({ state: "role" });
  });

  it("leaves err.gate undefined for a bare 403 from a raw HTTPException", async () => {
    // Reshaped { error: "HTTP-403", ... }: the #992 alias still runs, and the
    // placeholder code is not a gate signal.
    mockFetchOnce(403, {
      error: "HTTP-403",
      message: "Cannot modify your own role",
      details: {},
    });

    const err = await caught();
    expect(err.gate).toBeUndefined();
    expect(err.details?.detail).toBe("Cannot modify your own role");
  });

  it("leaves err.gate undefined for a legacy { detail } 403 or 429", async () => {
    mockFetchOnce(403, { detail: "Forbidden" });
    expect((await caught()).gate).toBeUndefined();

    mockFetchOnce(429, { detail: "Too many requests" });
    expect((await caught()).gate).toBeUndefined();
  });

  it("leaves err.gate undefined for a plain 500", async () => {
    mockFetchOnce(500, {
      error: "SYS-001",
      message: "Internal error",
      details: {},
    });

    expect((await caught()).gate).toBeUndefined();
  });

  it("leaves err.gate undefined on a network error", async () => {
    vi.spyOn(global, "fetch").mockRejectedValueOnce(new TypeError("offline"));

    const err = await caught();
    expect(err.status).toBe(0);
    expect(err.gate).toBeUndefined();
  });

  it("does not disturb the #992 422 errors->detail alias", async () => {
    const errors = [
      { loc: ["body", "name"], msg: "Field required", type: "missing" },
    ];
    mockFetchOnce(422, {
      error: "VAL-001",
      message: "Request validation failed",
      details: { errors },
    });

    const err = await caught();
    expect(err.gate).toBeUndefined();
    expect((err.details as Record<string, unknown>).detail).toEqual(errors);
  });

  it("reads the gate annotation on a refusal whose code did not move", async () => {
    mockFetchOnce(422, {
      error: "VAL-001",
      message: "Managed LLM is not configured on this deployment.",
      details: { gate: "deployment", feature: "managed_llm" },
    });

    expect((await caught()).gate).toEqual({
      state: "deployment",
      feature: "managed_llm",
    });
  });
});
