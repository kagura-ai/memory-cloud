#!/usr/bin/env node
/**
 * Mock OAuth IdP for the account-linking E2E harness (Issue #937).
 *
 * The backend performs the OAuth token + userinfo exchange *server-side*, so a
 * browser-level Playwright mock cannot intercept it. Instead, the backend is
 * pointed at THIS server via the `OAUTH_*_URL` overrides (gated behind
 * `OAUTH_ENDPOINT_OVERRIDE_ENABLED`, hard-blocked in production — see
 * backend/src/auth/oauth_endpoints.py).
 *
 * Zero dependencies (Node built-in `http` only) so it runs in CI without an
 * extra install step. Currently implements the GitHub provider surface, which
 * is the flow the #937 acceptance spec exercises ("Connect GitHub …"):
 *
 *   GET  /github/login/oauth/authorize   → 302 to redirect_uri?code=...&state=...
 *   POST /github/login/oauth/access_token → { access_token, token_type, scope }
 *   GET  /github/user                     → { id, login, name, avatar_url }
 *   GET  /github/user/emails              → [ { email, primary, verified } ]
 *
 * The returned identity is controllable via env so a test can drive a specific
 * sub/email:
 *   MOCK_IDP_PORT        (default 9100)
 *   MOCK_IDP_GH_SUB      (default "4242000")
 *   MOCK_IDP_GH_LOGIN    (default "e2e-octocat")
 *   MOCK_IDP_GH_EMAIL    (default "e2e-octocat@example.com")
 *   MOCK_IDP_GH_NAME     (default "E2E Octocat")
 */
import http from "node:http";

const PORT = Number(process.env.MOCK_IDP_PORT ?? 9100);
const GH = {
  sub: process.env.MOCK_IDP_GH_SUB ?? "4242000",
  login: process.env.MOCK_IDP_GH_LOGIN ?? "e2e-octocat",
  email: process.env.MOCK_IDP_GH_EMAIL ?? "e2e-octocat@example.com",
  name: process.env.MOCK_IDP_GH_NAME ?? "E2E Octocat",
};

const ACCESS_TOKEN = "mock-github-access-token";

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  // --- Health: lets Playwright's webServer readiness probe (GET /) succeed. ---
  if (req.method === "GET" && (path === "/" || path === "/health")) {
    sendJson(res, 200, { status: "ok", provider: "github" });
    return;
  }

  // --- GitHub authorize: bounce straight back to the backend callback. ---
  if (req.method === "GET" && path === "/github/login/oauth/authorize") {
    const redirectUri = url.searchParams.get("redirect_uri");
    const state = url.searchParams.get("state") ?? "";
    if (!redirectUri) {
      sendJson(res, 400, { error: "missing redirect_uri" });
      return;
    }
    const back = new URL(redirectUri);
    back.searchParams.set("code", "mock-github-code");
    back.searchParams.set("state", state);
    res.writeHead(302, { location: back.toString() });
    res.end();
    return;
  }

  // --- GitHub token exchange (backend posts here with Accept: application/json). ---
  if (req.method === "POST" && path === "/github/login/oauth/access_token") {
    // Drain the body; the mock does not validate client_id/secret/code.
    req.on("data", () => {});
    req.on("end", () => {
      sendJson(res, 200, {
        access_token: ACCESS_TOKEN,
        token_type: "bearer",
        scope: "read:user,user:email",
      });
    });
    return;
  }

  // --- GitHub user profile. ---
  if (req.method === "GET" && path === "/github/user") {
    sendJson(res, 200, {
      id: Number(GH.sub),
      login: GH.login,
      name: GH.name,
      avatar_url: `http://localhost:${PORT}/avatar.png`,
    });
    return;
  }

  // --- GitHub verified primary email (the backend trusts only this). ---
  if (req.method === "GET" && path === "/github/user/emails") {
    sendJson(res, 200, [
      { email: GH.email, primary: true, verified: true },
    ]);
    return;
  }

  sendJson(res, 404, { error: "not_found", path });
});

server.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`[mock-idp] listening on http://localhost:${PORT} (github)`);
});
