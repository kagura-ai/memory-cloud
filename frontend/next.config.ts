import type { NextConfig } from "next";

// `output: "standalone"` is only meaningful for production builds (next build);
// in dev mode (next dev) Next.js 16 + Turbopack tries to read static
// `build-manifest.json` files that the standalone path expects but the
// dev pipeline never writes — every request then 500s with ENOENT
// (`.next/dev/server/app/page/build-manifest.json`). Gate it on NODE_ENV so
// dev keeps Turbopack's incremental pipeline and prod still emits the
// standalone bundle for Docker.
const nextConfig: NextConfig = {
  ...(process.env.NODE_ENV === "production" ? { output: "standalone" } : {}),
  // WSL2 dev trap: when a Windows browser reaches the dev server via the
  // WSL IP (localhost forwarding broken in NAT mode), Next blocks the
  // cross-origin /_next asset requests from the unlisted origin and the
  // page silently never hydrates (no buttons, no API calls). Opt in per
  // environment with a comma-separated hostname list; dev-only knob.
  ...(process.env.NEXT_DEV_ALLOWED_ORIGINS
    ? { allowedDevOrigins: process.env.NEXT_DEV_ALLOWED_ORIGINS.split(",") }
    : {}),
  reactStrictMode: true,
  // #1588: /join/{token} carries a one-time beta invite token in its path
  // (#1581). `no-referrer` keeps that URL out of the Referer header on every
  // request the page makes (preview fetch, OAuth navigation, its own assets),
  // so the token cannot reach a proxy access log through a second field; the
  // invite URL must never be indexed either. app/join/layout.tsx repeats both
  // as <meta> for deployments whose proxy overwrites response headers.
  async headers() {
    return [
      {
        source: "/join/:path*",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
        ],
      },
    ];
  },
};

export default nextConfig;
