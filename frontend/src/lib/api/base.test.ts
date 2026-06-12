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
});
