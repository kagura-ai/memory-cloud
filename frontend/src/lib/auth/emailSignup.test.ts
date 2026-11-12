/**
 * Email + password sign-up client (#1734): the three calls send exactly the
 * bodies the backend contract names, and the return path stays in the browser
 * that started the sign-up (never in the request, never past its expiry,
 * never an unsafe value).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockPost = vi.fn();
vi.mock("@/lib/api/base", () => ({
  apiClient: { post: (...args: unknown[]) => mockPost(...args) },
}));
vi.mock("@/lib/auth/signupPrefs", () => ({
  signupPrefs: () => ({ locale: "ja", timezone: "Asia/Tokyo" }),
}));

import {
  clearSignupReturnTo,
  completeEmailSignup,
  peekSignupReturnTo,
  previewEmailSignup,
  rememberSignupReturnTo,
  requestEmailSignup,
  SIGNUP_RETURN_TO_KEY,
} from "./emailSignup";

const ORIGIN = "http://localhost:3000";

beforeEach(() => {
  mockPost.mockReset();
  mockPost.mockResolvedValue({});
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("requestEmailSignup", () => {
  it("posts the address with the page language and time zone", async () => {
    await requestEmailSignup({ email: "new@example.com" });
    expect(mockPost).toHaveBeenCalledWith("/api/v1/auth/signup/email/request", {
      email: "new@example.com",
      locale: "ja",
      timezone: "Asia/Tokyo",
    });
  });

  it("adds the beta invite token when there is one, and never a return path", async () => {
    await requestEmailSignup({ email: "new@example.com", invite: "inv_tok" });
    const body = mockPost.mock.calls[0][1] as Record<string, unknown>;
    expect(body.invite).toBe("inv_tok");
    expect(body).not.toHaveProperty("return_to");
  });
});

describe("previewEmailSignup", () => {
  it("posts the token in the body, not the URL", async () => {
    mockPost.mockResolvedValue({
      email: "new@example.com",
      expires_at: "2030-01-01T00:00:00Z",
    });
    const preview = await previewEmailSignup("tok-1");
    expect(mockPost).toHaveBeenCalledWith("/api/v1/auth/signup/email/preview", {
      token: "tok-1",
    });
    expect(preview.email).toBe("new@example.com");
  });
});

describe("completeEmailSignup", () => {
  it("maps the input to the backend's field names", async () => {
    mockPost.mockResolvedValue({ redirect_url: `${ORIGIN}/workspace/dashboard` });
    const result = await completeEmailSignup({
      token: "tok-1",
      newPassword: "Pw-123456789!",
      name: "Ada",
      acceptedTerms: "2026-01",
      returnTo: "/device?user_code=ABCD",
    });
    expect(mockPost).toHaveBeenCalledWith(
      "/api/v1/auth/signup/email/complete",
      {
        token: "tok-1",
        new_password: "Pw-123456789!",
        name: "Ada",
        accepted_terms: "2026-01",
        return_to: "/device?user_code=ABCD",
      },
    );
    expect(result.redirect_url).toBe(`${ORIGIN}/workspace/dashboard`);
  });

  it("leaves optional fields out when they are not given", async () => {
    await completeEmailSignup({ token: "tok-1", newPassword: "Pw-123456789!" });
    expect(mockPost.mock.calls[0][1]).toEqual({
      token: "tok-1",
      new_password: "Pw-123456789!",
    });
  });
});

describe("the remembered return path", () => {
  it("round-trips a safe path", () => {
    rememberSignupReturnTo("/device?user_code=ABCD");
    expect(peekSignupReturnTo(ORIGIN)).toBe("/device?user_code=ABCD");
  });

  it("is cleared by an empty value, so an older sign-up's path never applies", () => {
    rememberSignupReturnTo("/device");
    rememberSignupReturnTo(undefined);
    expect(peekSignupReturnTo(ORIGIN)).toBeUndefined();
    expect(window.localStorage.getItem(SIGNUP_RETURN_TO_KEY)).toBeNull();
  });

  it("expires after a day", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
    rememberSignupReturnTo("/device");
    vi.setSystemTime(new Date("2030-01-02T00:00:01Z"));
    expect(peekSignupReturnTo(ORIGIN)).toBeUndefined();
  });

  it("refuses an unsafe or malformed stored value", () => {
    window.localStorage.setItem(
      SIGNUP_RETURN_TO_KEY,
      JSON.stringify({ path: "//evil.example", expiresAt: Date.now() + 60_000 }),
    );
    expect(peekSignupReturnTo(ORIGIN)).toBeUndefined();
    window.localStorage.setItem(SIGNUP_RETURN_TO_KEY, "not json");
    expect(peekSignupReturnTo(ORIGIN)).toBeUndefined();
  });

  it("is removed by clearSignupReturnTo", () => {
    rememberSignupReturnTo("/device");
    clearSignupReturnTo();
    expect(window.localStorage.getItem(SIGNUP_RETURN_TO_KEY)).toBeNull();
  });

  it("never throws when storage is blocked", () => {
    // Blocked site data: reaching window.localStorage itself throws.
    const blocked = vi
      .spyOn(window, "localStorage", "get")
      .mockImplementation(() => {
        throw new Error("blocked");
      });
    try {
      expect(() => rememberSignupReturnTo("/device")).not.toThrow();
      expect(() => clearSignupReturnTo()).not.toThrow();
      expect(peekSignupReturnTo(ORIGIN)).toBeUndefined();
    } finally {
      blocked.mockRestore();
    }
  });
});
