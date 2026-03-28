'use client';

/**
 * WorkspaceCard Component
 *
 * Issue #276: Shared component for displaying workspace information.
 * Used by both WorkspaceSwitcher and WorkspaceSelectionScreen.
 */

import { PlanBadge } from '@/components/common/PlanBadge';
import { useTranslations } from 'next-intl';
import { Brain, Users } from 'lucide-react';

export interface WorkspaceInfo {
  id: string;
  name: string;
  description?: string | null;
  plan_name: string;
  member_count: number;
  context_count: number;
  current_user_role?: string | null;
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

  const baseClasses = `
    w-full text-left transition-all
    ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
  `;

  if (variant === 'compact') {
    return (
      <button
        onClick={onClick}
        disabled={disabled}
        className={`
          ${baseClasses}
          px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2
          ${isSelected ? 'bg-blue-50 dark:bg-blue-900' : ''}
        `}
      >
        <div className="w-6 h-6 rounded bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-xs font-bold flex-shrink-0">
          {workspace.name.charAt(0).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <div className="font-medium truncate">{workspace.name}</div>
            <PlanBadge planName={workspace.plan_name as 'free' | 'basic' | 'pro'} size="sm" />
            {workspace.current_user_role === 'owner' && (
              <span className="text-xs text-amber-500" title="Owner">👤</span>
            )}
          </div>
          <div className="text-xs text-gray-500 flex items-center gap-2">
            <span className="flex items-center gap-1">
              <Brain className="h-3 w-3" />
              {workspace.context_count}
            </span>
            <span className="flex items-center gap-1">
              <Users className="h-3 w-3" />
              {workspace.member_count}
            </span>
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
      </button>
    );
  }

  // Full variant for WorkspaceSelectionScreen
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`
        ${baseClasses}
        p-4 rounded-lg border-2 transition-all
        ${isSelected
          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
          : 'border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:hover:border-slate-600'
        }
        ${disabled ? '' : 'hover:shadow-md'}
      `}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-lg font-bold flex-shrink-0">
            {workspace.name.charAt(0).toUpperCase()}
          </div>
          <div className="text-left min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="font-semibold text-slate-900 dark:text-white truncate">
                {workspace.name}
              </h3>
              <PlanBadge planName={workspace.plan_name as 'free' | 'basic' | 'pro'} size="sm" />
              {workspace.current_user_role === 'owner' && (
                <span className="text-xs text-amber-500" title="Owner">👤</span>
              )}
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
    </button>
  );
}
