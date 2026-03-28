'use client';

/**
 * Workspace Context
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy
 *
 * Provides current workspace state to all dashboard pages.
 * Automatically redirects to workspace creation if user has no workspace.
 */

import { createContext, useContext, useEffect, useState, useRef, ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from './AuthContext';
import {
  listWorkspaces,
  switchWorkspace as switchWorkspaceApi,
  Workspace,
} from '@/lib/api/workspaces';

interface WorkspaceContextType {
  currentWorkspace: Workspace | null;
  currentWorkspaceId: string | null;
  workspaces: Workspace[];
  loading: boolean;
  refreshWorkspaces: () => Promise<void>;
  switchWorkspace: (workspaceId: string) => Promise<void>;
  setCreatedWorkspace: (workspace: Workspace) => void;
}

const WorkspaceContext = createContext<WorkspaceContextType | undefined>(undefined);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { user, isLoading: authLoading, refetchUser } = useAuth();
  const router = useRouter();

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  // Ref for immediate access to workspaces
  const workspacesRef = useRef<Workspace[]>([]);

  const loadWorkspaces = async (forceLoad = false) => {
    if (!user || authLoading) return;

    try {
      setLoading(true);
      const workspaces = await listWorkspaces();

      setWorkspaces(workspaces);
      workspacesRef.current = workspaces;

      // Set current workspace based on user's current_workspace_id
      // @ts-ignore - user.current_workspace_id might not be in type yet
      const userCurrentWorkspaceId = user?.current_workspace_id;
      if (userCurrentWorkspaceId && workspaces.length > 0) {
        const currentWorkspaceFromBackend = workspaces.find(o => o.id === userCurrentWorkspaceId);
        if (currentWorkspaceFromBackend) {
          setCurrentWorkspace(currentWorkspaceFromBackend);
        } else {
          setCurrentWorkspace(workspaces[0]);
        }
      } else if (workspaces.length > 0) {
        setCurrentWorkspace(workspaces[0]);
      }
      // Note: No automatic redirect - pages should handle "no workspace" case themselves
    } catch (error) {
      console.error('Failed to load workspaces:', error);
    } finally {
      setLoading(false);
    }
  };

  const refreshWorkspaces = async () => {
    await loadWorkspaces(true);
  };

  // Directly set workspace after creation (bypasses API call)
  const setCreatedWorkspace = (workspace: Workspace) => {
    workspacesRef.current = [workspace];
    setWorkspaces([workspace]);
    setCurrentWorkspace(workspace);
    setLoading(false);
  };

  const switchWorkspace = async (workspaceId: string) => {
    try {
      // Call API to update backend current_workspace_id
      await switchWorkspaceApi(workspaceId);

      // Refetch user data to get updated current_workspace_id
      await refetchUser();

      // Reload workspaces which will use the updated user.current_workspace_id
      await loadWorkspaces();

      // Refresh the page to reload all data with new workspace context
      router.refresh();
    } catch (error) {
      console.error('Failed to switch workspace:', error);
      throw error;
    }
  };

  useEffect(() => {
    loadWorkspaces();
  }, [user, authLoading]);

  const value: WorkspaceContextType = {
    currentWorkspace,
    currentWorkspaceId: currentWorkspace?.id || null,
    workspaces,
    loading,
    refreshWorkspaces,
    switchWorkspace,
    setCreatedWorkspace,
  };

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  const context = useContext(WorkspaceContext);
  if (context === undefined) {
    throw new Error('useWorkspace must be used within WorkspaceProvider');
  }
  return context;
}
