import { afterEach, describe, expect, it, vi } from "vitest";
import { buildOAuthRedirect } from "./buildOAuthRedirect";

// jsdom's window.location.origin; the helper resolves relative returnTo against it.
const FRONTEND_ORIGIN = "http://localhost:3000";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("buildOAuthRedirect — return_to absolute-URL conversion", () => {
  it("converts a relative path to absolute against window.location.origin", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("google", "/device?user_code=ABC");
    const parsed = new URL(url);
    expect(parsed.origin).toBe("https://api.example.com");
    expect(parsed.pathname).toBe("/api/v1/auth/google/login");
    expect(parsed.searchParams.get("return_to")).toBe(
      `${FRONTEND_ORIGIN}/device?user_code=ABC`,
    );
  });

  it("preserves an already-absolute same-origin URL verbatim", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const original = `${FRONTEND_ORIGIN}/invite/abc?x=1`;
    const url = buildOAuthRedirect("google", original);
    expect(new URL(url).searchParams.get("return_to")).toBe(original);
  });

  it("URL-encodes return_to so query characters survive transport", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("google", "/path?a=1&b=2");
    // The raw query string must contain the encoded form, not the literal &
    expect(url).toContain("return_to=");
    expect(url).toMatch(/return_to=http%3A%2F%2Flocalhost%3A3000%2Fpath/);
    expect(url).not.toMatch(/return_to=[^&]*&b=/); // not split into 2 params
  });
});

describe("buildOAuthRedirect — /api/v1 suffix strip on NEXT_PUBLIC_API_URL", () => {
  it("strips a trailing /api/v1 from the API base URL", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith("https://api.example.com/api/v1/auth/google/login"),
    ).toBe(true);
    expect(url).not.toContain("/api/v1/api/v1/");
  });

  it("strips a trailing /api/v1/ (with extra slash) from the API base URL", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1/");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith("https://api.example.com/api/v1/auth/google/login"),
    ).toBe(true);
    expect(url).not.toContain("/api/v1/api/v1/");
  });

  it("strips a trailing /api/v1 followed by multiple slashes", () => {
    // Regression guard for the Copilot-caught bug: prior regex `\/api\/v1\/?$`
    // only allowed 0 or 1 trailing slash, so `/api/v1///` slipped past and the
    // subsequent trailing-slash collapse left `/api/v1` intact → double-prefix
    // in the final URL.
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1///");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith("https://api.example.com/api/v1/auth/google/login"),
    ).toBe(true);
    expect(url).not.toContain("/api/v1/api/v1/");
  });

  it("leaves a non-suffix /api/v1 in the middle of the path alone", () => {
    // Hypothetical edge case: /api/v1 is part of the path, not at the end
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1/proxy");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith(
        "https://api.example.com/api/v1/proxy/api/v1/auth/google/login",
      ),
    ).toBe(true);
  });
});

describe("buildOAuthRedirect — trailing-slash collapse on NEXT_PUBLIC_API_URL", () => {
  it("collapses a single trailing slash", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith("https://api.example.com/api/v1/auth/google/login"),
    ).toBe(true);
    expect(url).not.toContain("https://api.example.com//");
  });

  it("collapses multiple trailing slashes", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com///");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith("https://api.example.com/api/v1/auth/google/login"),
    ).toBe(true);
  });
});

describe("buildOAuthRedirect — default apiBaseUrl fallback", () => {
  it("falls back to http://localhost:8080 when NEXT_PUBLIC_API_URL is unset", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const url = buildOAuthRedirect("google", "/x");
    expect(
      url.startsWith("http://localhost:8080/api/v1/auth/google/login"),
    ).toBe(true);
  });
});

describe("buildOAuthRedirect — both providers", () => {
  it("routes google to /api/v1/auth/google/login", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("google", "/x");
    expect(new URL(url).pathname).toBe("/api/v1/auth/google/login");
  });

  it("routes github to /api/v1/auth/github/login", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("github", "/x");
    expect(new URL(url).pathname).toBe("/api/v1/auth/github/login");
  });
});

describe("buildOAuthRedirect — returnTo validation (CWE-601 defense)", () => {
  it("throws TypeError on a cross-origin http URL", () => {
    expect(() => buildOAuthRedirect("google", "https://evil.com/x")).toThrow(
      TypeError,
    );
  });

  it("throws TypeError on a protocol-relative URL (resolves cross-origin)", () => {
    expect(() => buildOAuthRedirect("google", "//evil.com/x")).toThrow(
      TypeError,
    );
  });

  it("throws TypeError on a javascript: scheme", () => {
    expect(() => buildOAuthRedirect("google", "javascript:alert(1)")).toThrow(
      TypeError,
    );
  });

  it("throws TypeError on a data: scheme", () => {
    expect(() =>
      buildOAuthRedirect("google", "data:text/html,<script>alert(1)</script>"),
    ).toThrow(TypeError);
  });

  it("accepts an already-validated same-origin absolute URL", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    expect(() =>
      buildOAuthRedirect("google", `${FRONTEND_ORIGIN}/invite/abc`),
    ).not.toThrow();
  });
});
