'use client';

import { Crown, Users } from 'lucide-react';
import { useTranslations } from 'next-intl';

export function WorkspaceRelationship({ role }: { role?: string | null }) {
  const t = useTranslations('workspace');
  if (!role || !['owner', 'admin', 'member', 'viewer'].includes(role)) return null;
  const owned = role === 'owner';
  const Icon = owned ? Crown : Users;
  return <span className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${owned
    ? 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200'
    : 'bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-200'}`}>
    <Icon aria-hidden="true" className="h-3 w-3" />{t(owned ? 'ownedBadge' : 'sharedBadge')}
  </span>;
}
