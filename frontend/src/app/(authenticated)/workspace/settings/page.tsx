'use client';

/**
 * Workspace Settings Redirect
 *
 * Redirects to /workspace/settings/general for backwards compatibility.
 * This ensures old bookmarks and links continue to work.
 * Preserves query parameters (e.g., ?create=true for workspace creation).
 */

import { useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

export default function WorkspaceSettingsRedirect() {
  const router = useRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    // Preserve all query parameters when redirecting
    const queryString = searchParams.toString();
    const targetUrl = queryString
      ? `/workspace/settings/general?${queryString}`
      : '/workspace/settings/general';

    router.replace(targetUrl);
  }, [router, searchParams]);

  return null;
}
