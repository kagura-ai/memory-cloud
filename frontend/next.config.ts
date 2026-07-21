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
};

export default nextConfig;
