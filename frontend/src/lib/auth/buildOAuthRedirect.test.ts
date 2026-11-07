import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildOAuthRedirect } from "./buildOAuthRedirect";

// jsdom's window.location.origin; the helper resolves relative returnTo against it.
const FRONTEND_ORIGIN = "http://localhost:3000";

/** Make `Intl.DateTimeFormat()` report `timeZone` (or throw). */
function stubTimeZone(timeZone: string | Error) {
  vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function () {
    if (timeZone instanceof Error) throw timeZone;
    return { resolvedOptions: () => ({ timeZone }) };
  } as unknown as typeof Intl.DateTimeFormat);
}

// #2700: every URL ends with the sign-in page's language and the browser's
// time zone. Pinned here so the exact-URL assertions below do not depend on
// the machine running them.
const PREFS = "&locale=en&timezone=Asia%2FTokyo";

beforeEach(() => {
  localStorage.setItem("kagura_locale", "en");
  stubTimeZone("Asia/Tokyo");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  localStorage.clear();
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

describe("buildOAuthRedirect — optional invite token (#1582)", () => {
  it("appends &invite=<token> after return_to", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("google", "/", { invite: "tok_abc123" });
    expect(url).toBe(
      `https://api.example.com/api/v1/auth/google/login?return_to=${encodeURIComponent(`${FRONTEND_ORIGIN}/`)}&invite=tok_abc123${PREFS}`,
    );
  });

  it("URL-encodes the invite token", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("github", "/", { invite: "a b&c=d/e" });
    expect(url.endsWith(`&invite=a%20b%26c%3Dd%2Fe${PREFS}`)).toBe(true);
    expect(new URL(url).searchParams.get("invite")).toBe("a b&c=d/e");
  });

  it.each([
    ["no options", undefined],
    ["empty options", {}],
    ["undefined invite", { invite: undefined }],
    ["empty invite", { invite: "" }],
  ])("is byte-identical to the two-argument form with %s", (_label, options) => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    expect(buildOAuthRedirect("google", "/x?a=1", options)).toBe(
      buildOAuthRedirect("google", "/x?a=1"),
    );
    expect(buildOAuthRedirect("google", "/x?a=1", options)).not.toContain(
      "invite",
    );
  });

  it("still validates returnTo when an invite is supplied", () => {
    expect(() =>
      buildOAuthRedirect("google", "https://evil.com/x", { invite: "tok" }),
    ).toThrow(TypeError);
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

describe("buildOAuthRedirect — optional accepted terms version (#1665)", () => {
  it("appends &accepted_terms=<version> after the invite", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("github", "/", {
      invite: "tok",
      acceptedTerms: "2026-09",
    });
    expect(url).toBe(
      `https://api.example.com/api/v1/auth/github/login?return_to=${encodeURIComponent(`${FRONTEND_ORIGIN}/`)}&invite=tok&accepted_terms=2026-09${PREFS}`,
    );
  });

  it("URL-encodes the version", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    const url = buildOAuthRedirect("google", "/", { acceptedTerms: "a&b" });
    expect(new URL(url).searchParams.get("accepted_terms")).toBe("a&b");
  });

  it.each([
    ["undefined", { acceptedTerms: undefined }],
    ["empty", { acceptedTerms: "" }],
  ])("is byte-identical to the two-argument form with %s", (_l, options) => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    expect(buildOAuthRedirect("google", "/x", options)).toBe(
      buildOAuthRedirect("google", "/x"),
    );
  });
});

describe("buildOAuthRedirect — the sign-in page's language and time zone (#2700)", () => {
  it("sends the language chosen on the sign-in page", () => {
    localStorage.setItem("kagura_locale", "ja");
    const url = buildOAuthRedirect("google", "/");
    expect(new URL(url).searchParams.get("locale")).toBe("ja");
  });

  it("falls back to en when no language is stored", () => {
    localStorage.clear();
    vi.spyOn(navigator, "language", "get").mockReturnValue("de-DE");
    const url = buildOAuthRedirect("github", "/");
    expect(new URL(url).searchParams.get("locale")).toBe("en");
  });

  it("sends the browser's time zone", () => {
    const url = buildOAuthRedirect("google", "/");
    expect(new URL(url).searchParams.get("timezone")).toBe("Asia/Tokyo");
  });

  it("omits the time zone when Intl throws, and still sends the language", () => {
    vi.restoreAllMocks();
    stubTimeZone(new RangeError("no tz support"));
    const url = buildOAuthRedirect("google", "/");
    expect(url).not.toContain("timezone");
    expect(url.endsWith("&locale=en")).toBe(true);
  });

  it("URL-encodes the time zone", () => {
    vi.restoreAllMocks();
    // "+" unencoded would arrive as a space.
    stubTimeZone("Etc/GMT+9");
    const url = buildOAuthRedirect("google", "/");
    expect(url.endsWith("&timezone=Etc%2FGMT%2B9")).toBe(true);
    expect(new URL(url).searchParams.get("timezone")).toBe("Etc/GMT+9");
  });

  it("comes after the invite and the accepted terms version", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    localStorage.setItem("kagura_locale", "ja");
    const url = buildOAuthRedirect("google", "/x", {
      invite: "tok",
      acceptedTerms: "2026-09",
    });
    expect(url).toBe(
      `https://api.example.com/api/v1/auth/google/login?return_to=${encodeURIComponent(`${FRONTEND_ORIGIN}/x`)}&invite=tok&accepted_terms=2026-09&locale=ja&timezone=Asia%2FTokyo`,
    );
  });
});
