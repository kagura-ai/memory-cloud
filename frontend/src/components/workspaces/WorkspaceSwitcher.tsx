'use client';

/**
 * Workspace Switcher Component
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy Frontend
 * Issue #276: Added Create Workspace button and refactored to use WorkspaceCard
 *
 * Allows users to switch between workspaces and create new ones.
 */

import { useEffect, useId, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { ChevronsUpDown, Plus } from 'lucide-react';
import { WorkspaceRelationship } from './WorkspaceRelationship';
import { useWorkspace } from '@/contexts/WorkspaceContext';
import { PlanBadge } from '@/components/common/PlanBadge';
import type { PlanTier } from '@/lib/utils/planLabel';
import { useToast } from '@/hooks/use-toast';
import { WorkspaceCard } from './WorkspaceCard';

export function WorkspaceSwitcher() {
  const t = useTranslations('workspace');
  const { currentWorkspace, workspaces, loading, switchWorkspace } = useWorkspace();
  const [isOpen, setIsOpen] = useState(false);
  const menuId = useId();
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const router = useRouter();
  useEffect(() => {
    if (!isOpen) return;
    function closeOutside(event: PointerEvent) {
      if (event.target instanceof Node && !container.current?.contains(event.target)) setIsOpen(false);
    }
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, [isOpen]);
  const { toast } = useToast();
  const groups = [
    { title: t('ownedGroup'), items: workspaces.filter(ws => ws.current_user_role === 'owner') },
    { title: t('sharedGroup'), items: workspaces.filter(ws => ['admin', 'member', 'viewer'].includes(ws.current_user_role ?? '')) },
    { title: t('title'), items: workspaces.filter(ws => !['owner', 'admin', 'member', 'viewer'].includes(ws.current_user_role ?? '')) },
  ];

  const handleSwitchWorkspace = async (workspaceId: string) => {
    try {
      await switchWorkspace(workspaceId);
      setIsOpen(false);
    } catch (error) {
      console.error('Failed to switch workspace:', error);
      toast({
        title: t('switchFailed'),
        description: error instanceof Error ? error.message : undefined,
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
        {t("loadingWorkspaces")}
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
    <div ref={container} className="px-3 py-2 relative" onKeyDown={event => {
      if (event.key === "Escape" && isOpen) { event.stopPropagation(); setIsOpen(false); trigger.current?.focus(); }
    }}>
      <button
        ref={trigger}
        aria-expanded={isOpen}
        aria-controls={menuId}
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-3 py-2.5 text-sm text-left bg-gray-100 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center justify-between"
      >
        <div className="min-w-0 flex-1 space-y-1">
          <span className="sr-only">{t('currentWorkspaceCaption')}</span>
          <span className="block break-words text-sm font-semibold leading-5" title={currentWorkspace?.name}>{currentWorkspace?.name ?? t('selectWorkspace')}</span>
          {currentWorkspace && <span className="flex flex-wrap items-center gap-1.5">
            <WorkspaceRelationship role={currentWorkspace.current_user_role} />
            <PlanBadge planName={currentWorkspace.plan_name as PlanTier} size="sm" />
          </span>}
        </div>
        <ChevronsUpDown aria-hidden="true" className="ml-2 h-4 w-4 shrink-0 text-muted-foreground" />
      </button>

      {isOpen && (
        <div role="group" aria-label={t("title")} id={menuId} className="absolute left-3 right-3 mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded shadow-lg z-50 max-h-80 overflow-y-auto">
          {/* Dropdown Header */}
          <div className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide border-b border-gray-200 dark:border-gray-700">
            {t('title')}
          </div>

          {/* Workspace List */}
          {groups.filter(group => group.items.length > 0).map(group => <div key={group.title}>
            <div className="px-3 pb-1 pt-3 text-xs font-medium text-muted-foreground">{group.title}</div>
            {group.items.map(workspace => <WorkspaceCard
              key={workspace.id} workspace={workspace} isSelected={currentWorkspace?.id === workspace.id}
              variant="compact" onClick={() => handleSwitchWorkspace(workspace.id)}
            />)}
          </div>)}

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
