/**
 * Next.js Proxy
 *
 * Issue #651 - Minimal proxy for Next.js 16
 * Migrated from middleware.ts per Next.js 16 deprecation
 * Menu Restructure - URL redirects for backward compatibility
 *
 * Note: Authentication guards are handled in (authenticated)/layout.tsx
 */

import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// URL redirects for menu restructure
// Supports both exact matches and wildcard (child routes)
interface RedirectRule {
  from: string;
  to: string;
  preservePath?: boolean;  // If true, append remaining path to destination
}

const REDIRECTS: RedirectRule[] = [
  { from: '/workspace/credentials', to: '/workspace/oauth-apps', preservePath: true },
  { from: '/workspace/schemas', to: '/workspace/developer/resource-tokens', preservePath: true },
  { from: '/workspace/resource-tokens', to: '/workspace/developer/resource-tokens', preservePath: true },
  { from: '/workspace/external-keys', to: '/workspace/settings/external-keys', preservePath: true },
  { from: '/workspace/plan', to: '/workspace/settings/plan', preservePath: true },
  { from: '/workspace/billing', to: '/workspace/settings/billing', preservePath: true },
];

export default async function proxy(request: NextRequest) {
  const pathname = request.nextUrl.pathname;

  // Check redirects (exact match or prefix match if preservePath)
  for (const redirect of REDIRECTS) {
    if (pathname === redirect.from) {
      // Exact match - redirect with query params
      const redirectUrl = new URL(redirect.to + request.nextUrl.search, request.url);
      return NextResponse.redirect(redirectUrl);
    } else if (redirect.preservePath && pathname.startsWith(redirect.from + '/')) {
      // Prefix match - preserve remaining path
      const remainingPath = pathname.substring(redirect.from.length);

      // Security: Prevent path traversal attacks
      if (remainingPath.includes('..')) {
        return NextResponse.next();
      }

      // Normalize slashes to prevent double slashes
      const normalizedTo = redirect.to.endsWith('/') ? redirect.to.slice(0, -1) : redirect.to;
      const normalizedRemaining = remainingPath.startsWith('/') ? remainingPath : '/' + remainingPath;

      const redirectUrl = new URL(
        normalizedTo + normalizedRemaining + request.nextUrl.search,
        request.url
      );
      return NextResponse.redirect(redirectUrl);
    }
  }

  // All other requests pass through
  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * Match all request paths except for the ones starting with:
     * - api (API routes)
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     */
    '/((?!api|_next/static|_next/image|favicon.ico).*)',
  ],
};
