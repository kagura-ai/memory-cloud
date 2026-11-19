'use client';

/**
 * WorkspaceCard Component
 *
 * Issue #276: Shared component for displaying workspace information.
 * Used by both WorkspaceSwitcher and WorkspaceSelectionScreen.
 *
 * Issue #2919: a workspace suspended over its owner's allowance
 * (`slot_suspended`) is shown, labelled and never selectable. Under the card
 * the owner gets a link to Workspace management, a member the advice to ask
 * the owner (a link cannot sit inside the card's button).
 */

import Link from 'next/link';
import { PlanBadge } from '@/components/common/PlanBadge';
import { Badge } from '@/components/ui/badge';
import type { PlanTier } from '@/lib/utils/planLabel';
import { useTranslations } from 'next-intl';
import { Brain, Users } from 'lucide-react';
import { WorkspaceMark, WorkspaceRelationship } from './WorkspaceRelationship';

export interface WorkspaceInfo {
  id: string;
  name: string;
  description?: string | null;
  plan_name: string;
  member_count: number;
  context_count: number;
  current_user_role?: string | null;
  /** #2918: suspended over the owner's workspace allowance. */
  slot_suspended?: boolean;
}

interface WorkspaceCardProps {
  workspace: WorkspaceInfo;
  isSelected?: boolean;
  variant?: 'compact' | 'full';
  onClick?: () => void;
  disabled?: boolean;
}

export function WorkspaceCard({
  workspace,
  isSelected = false,
  variant = 'full',
  onClick,
  disabled = false,
}: WorkspaceCardProps) {
  const t = useTranslations('workspace');
  const tSlots = useTranslations('workspaceSlots');
  const suspended = workspace.slot_suspended === true;
  const isDisabled = disabled || suspended;

  const baseClasses = `
    w-full text-left transition-all
    ${isDisabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
  `;

  const suspendedBadge = suspended ? (
    <Badge variant="outline" className="text-xs">
      {tSlots('card.suspended')}
    </Badge>
  ) : null;

  const withSuspendedNote = (card: React.ReactNode, compact: boolean) => {
    if (!suspended) return card;
    return (
      <div>
        {card}
        <p className={`${compact ? 'px-3 pb-2' : 'px-4 pt-1'} text-xs text-slate-600 dark:text-slate-300`}>
          {workspace.current_user_role === 'owner' ? (
            <Link href="/profile/workspaces" className="underline">
              {tSlots('card.manage')}
            </Link>
          ) : (
            tSlots('card.askOwner')
          )}
        </p>
      </div>
    );
  };

  if (variant === 'compact') {
    return withSuspendedNote(
      <button
        onClick={onClick}
        disabled={isDisabled}
        aria-current={isSelected ? true : undefined}
        className={`
          ${baseClasses}
          px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2
          ${isSelected ? 'bg-blue-50 dark:bg-blue-900' : ''}
        `}
      >
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium" title={workspace.name}>{workspace.name}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <WorkspaceRelationship role={workspace.current_user_role} />
            <PlanBadge planName={workspace.plan_name as PlanTier} size="sm" />
            {suspendedBadge}
          </div>
        </div>
        {isSelected && (
          <svg className="w-4 h-4 text-blue-600 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path
              fillRule="evenodd"
              d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
              clipRule="evenodd"
            />
          </svg>
        )}
      </button>,
      true,
    );
  }

  // Full variant for WorkspaceSelectionScreen
  return withSuspendedNote(
    <button
      onClick={onClick}
      disabled={isDisabled}
      aria-current={isSelected ? true : undefined}
      className={`
        ${baseClasses}
        p-4 rounded-lg border-2 transition-all
        ${isSelected
          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
          : 'border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:hover:border-slate-600'
        }
        ${isDisabled ? '' : 'hover:shadow-md'}
      `}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-3">
          <WorkspaceMark role={workspace.current_user_role} className="h-10 w-10" />
          <div className="text-left min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="font-semibold text-slate-900 dark:text-white truncate">
                {workspace.name}
              </h3>
              <PlanBadge planName={workspace.plan_name as PlanTier} size="sm" />
              <WorkspaceRelationship role={workspace.current_user_role} />
              {suspendedBadge}
            </div>
            {workspace.description && (
              <p className="text-sm text-slate-600 dark:text-slate-400 mb-2 line-clamp-2">
                {workspace.description}
              </p>
            )}
            <div className="flex items-center gap-4 text-sm text-slate-500 dark:text-slate-400">
              <span className="flex items-center gap-1">
                <Brain className="h-4 w-4" />
                {workspace.context_count}
              </span>
              <span className="flex items-center gap-1">
                <Users className="h-4 w-4" />
                {workspace.member_count}
              </span>
              {workspace.current_user_role && (
                <span className="capitalize text-blue-600 dark:text-blue-400">
                  {workspace.current_user_role}
                </span>
              )}
            </div>
          </div>
        </div>
        {isSelected && (
          <svg className="w-5 h-5 text-blue-600 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path
              fillRule="evenodd"
              d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
              clipRule="evenodd"
            />
          </svg>
        )}
      </div>
    </button>,
    false,
  );
}
