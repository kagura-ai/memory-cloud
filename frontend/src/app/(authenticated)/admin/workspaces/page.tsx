'use client';

/**
 * System Admin - Workspaces Management Page - Coming Soon
 *
 * Issue #115 - Workspace-level Multi-tenancy Support
 */

import { useTranslations } from 'next-intl';
import { ComingSoon } from '@/components/common/ComingSoon';

export default function AdminWorkspacesPage() {
  const t = useTranslations('admin.workspaces');

  return (
    <ComingSoon
      title={t('title')}
      description={t('description')}
      featureDescription={t('featureDescription')}
    />
  );
}
