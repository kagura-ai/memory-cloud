'use client';

/**
 * WorkspaceSelectionScreen Component
 *
 * Issue #276: Full-screen workspace selection for users with multiple workspaces.
 * Displayed when user has multiple workspaces and current_workspace_id is not set.
 */

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { Plus } from 'lucide-react';
import { WorkspaceCard, WorkspaceInfo } from './WorkspaceCard';

const LAST_WORKSPACE_KEY = 'kagura_last_workspace_id';

interface WorkspaceSelectionScreenProps {
  workspaces: WorkspaceInfo[];
  onSelect: (workspaceId: string) => Promise<void>;
  onCreateNew?: () => void;
}

export function WorkspaceSelectionScreen({
  workspaces,
  onSelect,
  onCreateNew,
}: WorkspaceSelectionScreenProps) {
  const t = useTranslations('workspace');
  const router = useRouter();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Check localStorage for last used workspace
  useEffect(() => {
    const lastWorkspaceId = localStorage.getItem(LAST_WORKSPACE_KEY);
    if (lastWorkspaceId && workspaces.some(w => w.id === lastWorkspaceId)) {
      setSelectedId(lastWorkspaceId);
    }
  }, [workspaces]);

  const handleSelect = async (workspaceId: string) => {
    // Validate workspace still exists before switching
    const workspace = workspaces.find(w => w.id === workspaceId);
    if (!workspace) {
      console.error('Workspace not found:', workspaceId);
      localStorage.removeItem(LAST_WORKSPACE_KEY);
      return;
    }

    setLoading(true);
    setSelectedId(workspaceId);
    try {
      await onSelect(workspaceId);
      // Save to localStorage AFTER successful switch (not before)
      localStorage.setItem(LAST_WORKSPACE_KEY, workspaceId);
    } catch (error) {
      console.error('Failed to switch workspace:', error);
      // Clear invalid selection on error
      localStorage.removeItem(LAST_WORKSPACE_KEY);
      setLoading(false);
    }
  };

  const handleCreateNew = () => {
    if (onCreateNew) {
      onCreateNew();
    } else {
      router.push('/workspace/settings?create=true');
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900 flex items-center justify-center p-6">
      <div className="max-w-2xl w-full">
        {/* Header with Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-500 to-purple-600 mb-4">
            <svg className="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">
            {t('selectWorkspace')}
          </h1>
          <p className="text-slate-600 dark:text-slate-400 mt-2">
            {t('selectWorkspaceDesc')}
          </p>
        </div>

        {/* Workspace Grid */}
        <div className="space-y-3">
          {workspaces.map((workspace) => (
            <WorkspaceCard
              key={workspace.id}
              workspace={workspace}
              isSelected={selectedId === workspace.id}
              variant="full"
              onClick={() => handleSelect(workspace.id)}
              disabled={loading}
            />
          ))}

          {/* Create New Workspace Button */}
          <button
            onClick={handleCreateNew}
            disabled={loading}
            className={`
              w-full p-4 rounded-lg border-2 border-dashed
              border-slate-300 dark:border-slate-600
              text-center transition-colors
              ${loading
                ? 'opacity-50 cursor-not-allowed'
                : 'hover:border-slate-400 dark:hover:border-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800'
              }
            `}
          >
            <Plus className="h-6 w-6 mx-auto mb-2 text-slate-400" />
            <span className="text-slate-600 dark:text-slate-400">
              {t('createNewWorkspace')}
            </span>
          </button>
        </div>

        {/* Last used hint */}
        {selectedId && (
          <p className="text-center text-sm text-slate-500 dark:text-slate-400 mt-4">
            {t('lastUsedWorkspace')}
          </p>
        )}
      </div>
    </div>
  );
}
