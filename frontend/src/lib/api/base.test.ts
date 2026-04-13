import { describe, it, expect } from "vitest";
import { ApiError } from "./base";

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
