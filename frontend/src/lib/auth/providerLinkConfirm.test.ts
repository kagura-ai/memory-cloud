/**
 * Provider link confirmation client (#2786): the three calls post the token in
 * a JSON body (never the URL) through the shared client, which sends cookies:
 * the browser cookie set at /auth/{provider}/login is what lets `complete`
 * connect anything.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  cancelProviderLink,
  completeProviderLink,
  isAllowedProviderAvatar,
  previewProviderLink,
} from "./providerLinkConfirm";

const TOKEN = "tok_SECRET_link_confirm";
const fetchMock = vi.fn();

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function lastCall(): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return { url, init };
}

describe("previewProviderLink", () => {
  it("posts the token in the body with cookies and returns the preview", async () => {
    const preview = {
      provider: "github",
      provider_login: "octo-cat",
      provider_name: "Octo Cat",
      provider_avatar_url: null,
      expires_at: "2026-11-12T10:15:00Z",
      same_browser: true,
    };
    fetchMock.mockResolvedValue(jsonResponse(200, preview));

    await expect(previewProviderLink(TOKEN)).resolves.toEqual(preview);

    const { url, init } = lastCall();
    expect(url.endsWith("/api/v1/auth/link-confirm/preview")).toBe(true);
    expect(url).not.toContain(TOKEN);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({ token: TOKEN });
  });
});

describe("completeProviderLink", () => {
  it("posts the token with cookies and returns where to go next", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { redirect_url: "/workspace/dashboard" }),
    );

    await expect(completeProviderLink(TOKEN)).resolves.toEqual({
      redirect_url: "/workspace/dashboard",
    });

    const { url, init } = lastCall();
    expect(url.endsWith("/api/v1/auth/link-confirm/complete")).toBe(true);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({ token: TOKEN });
  });

  it("surfaces the status and code of a refusal", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(403, { error: "AUTH-310", message: "x", details: {} }),
    );

    await expect(completeProviderLink(TOKEN)).rejects.toMatchObject({
      status: 403,
      error: "AUTH-310",
    });
  });
});

describe("cancelProviderLink", () => {
  it("posts the token with cookies and accepts the empty 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(cancelProviderLink(TOKEN)).resolves.toBeUndefined();

    const { url, init } = lastCall();
    expect(url.endsWith("/api/v1/auth/link-confirm/cancel")).toBe(true);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({ token: TOKEN });
  });
});

describe("isAllowedProviderAvatar", () => {
  it.each([
    ["github", "https://avatars.githubusercontent.com/u/1?v=4", true],
    ["google", "https://lh3.googleusercontent.com/a/abc=s96-c", true],
    // Each provider's own host only.
    ["github", "https://lh3.googleusercontent.com/a/abc", false],
    ["google", "https://avatars.githubusercontent.com/u/1", false],
    // https only, exact host, no credentials or ports.
    ["github", "http://avatars.githubusercontent.com/u/1", false],
    ["github", "https://avatars.githubusercontent.com.example.com/u/1", false],
    ["github", "https://evil.example.com/avatars.githubusercontent.com", false],
    ["github", "https://user@avatars.githubusercontent.com/u/1", false],
    ["github", "https://avatars.githubusercontent.com:8443/u/1", false],
    ["github", "javascript:alert(1)", false],
    ["github", "not a url", false],
    ["github", null, false],
    ["github", "", false],
  ] as const)("%s %s → %s", (provider, url, allowed) => {
    expect(isAllowedProviderAvatar(provider, url)).toBe(allowed);
  });
});
