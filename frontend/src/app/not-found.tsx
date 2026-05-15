import Link from "next/link";

// Explicit not-found page to work around a Next.js 16.2.6 + Turbopack
// regression where the default `/_not-found` route fails during Docker
// production builds with `Error: Failed to collect configuration for
// /_not-found, TypeError: Invalid URL` (Issue #643). Local builds outside
// Docker were not affected; the difference appears to be musl vs glibc
// libc on the build node. Providing a static, metadata-free page
// pre-empts Next.js's auto-generation of the failing route.
export const metadata = {
  title: "404 — Page Not Found",
};

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-6 py-12 text-center">
      <h1 className="mb-4 text-6xl font-bold tracking-tight">404</h1>
      <p className="mb-8 text-lg text-gray-600 dark:text-gray-400">
        The page you are looking for does not exist.
      </p>
      <Link
        href="/"
        className="rounded-md bg-blue-600 px-6 py-2 text-white transition hover:bg-blue-700"
      >
        Return home
      </Link>
    </div>
  );
}
