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
  reactStrictMode: true,
};

export default nextConfig;
