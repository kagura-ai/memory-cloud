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
