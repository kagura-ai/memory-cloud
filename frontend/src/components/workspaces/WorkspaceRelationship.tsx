'use client';

import { Building2, UserRound, Users } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { useTranslations } from 'next-intl';

export function WorkspaceRelationship({ role }: { role?: string | null }) {
  const t = useTranslations('workspace');
  if (!role || !['owner', 'admin', 'member', 'viewer'].includes(role)) return null;
  const owned = role === 'owner';
  return <span className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${owned
    ? 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200'
    : 'bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-200'}`}>
    {t(owned ? 'ownedBadge' : 'sharedBadge')}
  </span>;
}

export function WorkspaceMark({ role, className = 'h-8 w-8' }: { role?: string | null; className?: string }) {
  const { user } = useAuth();
  const owned = role === 'owner';
  const shared = role === 'admin' || role === 'member' || role === 'viewer';
  const Icon = owned ? UserRound : shared ? Users : Building2;
  return <Avatar aria-hidden="true" className={`shrink-0 rounded-md ${className}`}>
    {owned && user?.picture && <AvatarImage src={user.picture} alt="" />}
    <AvatarFallback className={`rounded-md ${shared ? 'bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-200' : 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-100'}`}>
      <Icon className="h-4 w-4" />
    </AvatarFallback>
  </Avatar>;
}
