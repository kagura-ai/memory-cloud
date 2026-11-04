'use client';

/**
 * Workspace Settings Redirect
 *
 * Opens Plan by default when enabled, or General for disabled Plan deployments.
 * This ensures old bookmarks and links continue to work.
 * Preserves query parameters (e.g., ?create=true for workspace creation).
 */

import { useEffect } from 'react';
import { useSystemFeatures } from '@/hooks/useSystemFeatures';
import { useRouter, useSearchParams } from 'next/navigation';

export default function WorkspaceSettingsRedirect() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const features = useSystemFeatures();

  useEffect(() => {
    const creating = searchParams.get("create") === "true";
    if (!creating && features === null) return;
    const destination = !creating && features?.plan_page === true
      ? "/workspace/settings/plan" : "/workspace/settings/general";
    // Preserve all query parameters when redirecting
    const queryString = searchParams.toString();
    const targetUrl = queryString
      ? `${destination}?${queryString}`
      : destination;

    router.replace(targetUrl);
  }, [router, searchParams, features]);

  return null;
}
