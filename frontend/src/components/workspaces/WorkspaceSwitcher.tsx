'use client';

/**
 * Workspace Switcher Component
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy Frontend
 * Issue #276: Added Create Workspace button and refactored to use WorkspaceCard
 *
 * Allows users to switch between workspaces and create new ones.
 */

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { Plus } from 'lucide-react';
import { useWorkspace } from '@/contexts/WorkspaceContext';
import { PlanBadge } from '@/components/common/PlanBadge';
import { useToast } from '@/hooks/use-toast';
import { WorkspaceCard } from './WorkspaceCard';

export function WorkspaceSwitcher() {
  const t = useTranslations('workspace');
  const { currentWorkspace, workspaces, loading, switchWorkspace } = useWorkspace();
  const [isOpen, setIsOpen] = useState(false);
  const router = useRouter();
  const { toast } = useToast();

  const handleSwitchWorkspace = async (workspaceId: string) => {
    try {
      await switchWorkspace(workspaceId);
      setIsOpen(false);
    } catch (error) {
      console.error('Failed to switch workspace:', error);
      toast({
        title: 'Error',
        description: 'Failed to switch workspace',
        variant: 'destructive',
      });
    }
  };

  const handleCreateWorkspace = () => {
    setIsOpen(false);
    router.push('/workspace/settings?create=true');
  };

  if (loading) {
    return (
      <div className="px-3 py-2 text-sm text-gray-500">
        Loading workspaces...
      </div>
    );
  }

  if (workspaces.length === 0) {
    return (
      <div className="px-3 py-2">
        <button
          onClick={handleCreateWorkspace}
          className="w-full px-3 py-2 text-sm text-left bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          {t('createWorkspace')}
        </button>
      </div>
    );
  }

  return (
    <div className="px-3 py-2 relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-3 py-2 text-sm text-left bg-gray-100 dark:bg-gray-800 rounded hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center justify-between"
      >
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <div className="w-6 h-6 rounded bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-xs font-bold">
            {currentWorkspace?.name.charAt(0).toUpperCase()}
          </div>
          <span className="font-medium truncate">{currentWorkspace?.name}</span>
          {currentWorkspace && <PlanBadge planName={currentWorkspace.plan_name as 'free' | 'basic' | 'pro'} size="sm" />}
        </div>
        <svg
          className={`w-4 h-4 transition-transform ${isOpen ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute left-3 right-3 mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-lg z-50 max-h-80 overflow-y-auto">
          {/* Dropdown Header */}
          <div className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide border-b border-gray-200 dark:border-gray-700">
            {t('title')}
          </div>

          {/* Workspace List */}
          {workspaces.map((workspace) => (
            <WorkspaceCard
              key={workspace.id}
              workspace={workspace}
              isSelected={currentWorkspace?.id === workspace.id}
              variant="compact"
              onClick={() => handleSwitchWorkspace(workspace.id)}
            />
          ))}

          {/* Issue #276: Create Workspace Button */}
          <div className="border-t border-gray-200 dark:border-gray-700">
            <button
              onClick={handleCreateWorkspace}
              className="w-full px-3 py-2 text-sm text-left text-blue-600 dark:text-blue-400 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2"
            >
              <Plus className="w-4 h-4" />
              {t('createNewWorkspace')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
