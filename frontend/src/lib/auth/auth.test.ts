import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockApiClientGet = vi.fn();
const mockApiClientPost = vi.fn();

vi.mock("../api/base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockApiClientGet(...args),
    post: (...args: unknown[]) => mockApiClientPost(...args),
  },
  ApiError: class ApiError extends Error {},
}));

import {
  getAuthUrl,
  getGitHubAuthUrl,
  loginWithPassword,
  verifyMfa,
} from "./auth";

beforeEach(() => {
  mockApiClientGet.mockReset();
  mockApiClientPost.mockReset();
  mockApiClientGet.mockResolvedValue({ authorization_url: "https://idp/" });
  mockApiClientPost.mockResolvedValue({ success: true, mfa_required: false });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getAuthUrl — no return_to query param appended (post-#775 contract)", () => {
  it("calls /api/v1/auth/google/login with no query string", async () => {
    await getAuthUrl();
    expect(mockApiClientGet).toHaveBeenCalledWith("/api/v1/auth/google/login");
  });
});

describe("getGitHubAuthUrl — no return_to query param appended (post-#775 contract)", () => {
  it("calls /api/v1/auth/github/login with no query string", async () => {
    await getGitHubAuthUrl();
    expect(mockApiClientGet).toHaveBeenCalledWith("/api/v1/auth/github/login");
  });
});

describe("loginWithPassword — return_to encoding via returnToParam", () => {
  it("omits ?return_to when returnTo is not provided", async () => {
    await loginWithPassword("user", "pass");
    expect(mockApiClientPost).toHaveBeenCalledWith("/api/v1/auth/login", {
      login_id: "user",
      password: "pass",
    });
  });

  it("appends ?return_to with encoded value when returnTo is a simple path", async () => {
    await loginWithPassword("user", "pass", "/dashboard");
    expect(mockApiClientPost).toHaveBeenCalledWith(
      "/api/v1/auth/login?return_to=%2Fdashboard",
      { login_id: "user", password: "pass" },
    );
  });

  it("encodes URL-special characters (& ? # =) so they survive transport", async () => {
    await loginWithPassword("user", "pass", "/foo?bar=baz&qux#frag");
    expect(mockApiClientPost).toHaveBeenCalledWith(
      "/api/v1/auth/login?return_to=%2Ffoo%3Fbar%3Dbaz%26qux%23frag",
      { login_id: "user", password: "pass" },
    );
  });

  it("encodes whitespace (space, tab, newline)", async () => {
    await loginWithPassword("user", "pass", "/path with\ttab\nnewline");
    const url = mockApiClientPost.mock.calls[0][0];
    expect(url).toBe(
      "/api/v1/auth/login?return_to=%2Fpath%20with%09tab%0Anewline",
    );
  });

  it("encodes multi-byte Unicode characters (Japanese)", async () => {
    await loginWithPassword("user", "pass", "/ワークスペース");
    const url = mockApiClientPost.mock.calls[0][0];
    expect(url).toBe(
      "/api/v1/auth/login?return_to=%2F%E3%83%AF%E3%83%BC%E3%82%AF%E3%82%B9%E3%83%9A%E3%83%BC%E3%82%B9",
    );
  });

  it("encodes emoji (4-byte UTF-8)", async () => {
    await loginWithPassword("user", "pass", "/🚀");
    const url = mockApiClientPost.mock.calls[0][0];
    expect(url).toBe("/api/v1/auth/login?return_to=%2F%F0%9F%9A%80");
  });

  it("double-encodes already-encoded sequences (caller contract: pass raw paths)", async () => {
    // encodeURIComponent("%20") → "%2520". Documents the one-way encoder contract.
    // Callers should pass raw paths; pre-encoded input is treated as literal text.
    await loginWithPassword("user", "pass", "/foo%20bar");
    const url = mockApiClientPost.mock.calls[0][0];
    expect(url).toBe("/api/v1/auth/login?return_to=%2Ffoo%2520bar");
  });
});

describe("verifyMfa — return_to encoding delegates to returnToParam (smoke)", () => {
  it("omits ?return_to when returnTo is not provided", async () => {
    await verifyMfa("session-token", "123456");
    expect(mockApiClientPost).toHaveBeenCalledWith("/api/v1/auth/mfa/verify", {
      mfa_session_token: "session-token",
      totp_code: "123456",
    });
  });

  it("appends ?return_to with encoded value when returnTo is provided", async () => {
    await verifyMfa("session-token", "123456", "/foo?bar=baz");
    expect(mockApiClientPost).toHaveBeenCalledWith(
      "/api/v1/auth/mfa/verify?return_to=%2Ffoo%3Fbar%3Dbaz",
      { mfa_session_token: "session-token", totp_code: "123456" },
    );
  });
});
