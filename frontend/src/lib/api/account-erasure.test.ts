/**
 * Tests for the self-serve account erasure API client (Issue #953).
 *
 * Verifies the four wrappers hit the correct SessionUser endpoints with the
 * right bodies, and the cooling-off predicate, against the backend
 * (`backend/src/api/routes/me_account.py`).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

import {
  requestErasure,
  confirmErasure,
  cancelErasure,
  getActiveErasureRequest,
  erasureStage,
  type ErasureRequestState,
} from "./account-erasure";

const REQUEST = "/api/v1/me/account/erasure-request";
const CONFIRM = "/api/v1/me/account/erasure-confirm";

function state(over: Partial<ErasureRequestState> = {}): ErasureRequestState {
  return {
    request_id: "r1",
    status: "cooling_off",
    is_self_service: true,
    requested_at: "2026-06-13T00:00:00Z",
    confirmed_at: "2026-06-13T00:01:00Z",
    scheduled_for: "2026-06-20T00:01:00Z",
    started_at: null,
    completed_at: null,
    cancelled_at: null,
    failure_reason: null,
    ...over,
  };
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
});

describe("requestErasure", () => {
  it("POSTs the erasure-request endpoint with no body", async () => {
    mockPost.mockResolvedValue({ request_id: "r1", status: "pending", requested_at: "x", confirm_token: "tok" });
    const res = await requestErasure();
    expect(mockPost).toHaveBeenCalledWith(REQUEST);
    expect(res.confirm_token).toBe("tok");
  });
});

describe("confirmErasure", () => {
  it("includes the password for password-auth users", async () => {
    mockPost.mockResolvedValue(state());
    await confirmErasure("tok", "hunter2");
    expect(mockPost).toHaveBeenCalledWith(CONFIRM, { token: "tok", password: "hunter2" });
  });

  it("omits the password for OAuth users (token only)", async () => {
    mockPost.mockResolvedValue(state());
    await confirmErasure("tok");
    expect(mockPost).toHaveBeenCalledWith(CONFIRM, { token: "tok" });
  });

  it("omits an empty-string password rather than sending it", async () => {
    mockPost.mockResolvedValue(state());
    await confirmErasure("tok", "");
    expect(mockPost).toHaveBeenCalledWith(CONFIRM, { token: "tok" });
  });
});

describe("cancelErasure", () => {
  it("DELETEs the erasure-request endpoint", async () => {
    mockDelete.mockResolvedValue(state({ status: "cancelled", cancelled_at: "x" }));
    await cancelErasure();
    expect(mockDelete).toHaveBeenCalledWith(REQUEST);
  });
});

describe("getActiveErasureRequest", () => {
  it("GETs the erasure-request endpoint", async () => {
    mockGet.mockResolvedValue(null);
    const res = await getActiveErasureRequest();
    expect(mockGet).toHaveBeenCalledWith(REQUEST);
    expect(res).toBeNull();
  });
});

describe("erasureStage", () => {
  it("is 'none' for null", () => {
    expect(erasureStage(null)).toBe("none");
  });
  it("is 'pending' for an unconfirmed request", () => {
    expect(erasureStage(state({ status: "pending", confirmed_at: null, scheduled_for: null }))).toBe("pending");
  });
  it("is 'cooling_off' for a confirmed, scheduled request", () => {
    expect(erasureStage(state())).toBe("cooling_off");
  });
  it("is 'in_progress' for an executing request (NOT mistaken for cooling_off)", () => {
    // in_progress still carries confirmed_at + scheduled_for, but must not be
    // treated as cancellable cooling-off.
    expect(erasureStage(state({ status: "in_progress", started_at: "2026-06-20T00:02:00Z" }))).toBe("in_progress");
  });
  it("is 'none' for a terminal status the endpoint shouldn't surface", () => {
    expect(erasureStage(state({ status: "cancelled" }))).toBe("none");
  });
});
