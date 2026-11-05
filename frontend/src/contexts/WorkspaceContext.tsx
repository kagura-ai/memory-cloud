'use client';

/**
 * Workspace Context
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy
 *
 * Provides current workspace state to all dashboard pages.
 * Automatically redirects to workspace creation if user has no workspace.
 */

import { createContext, useContext, useEffect, useState, useCallback, useRef, ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { ApiError } from '@/lib/api/base';
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
  loadFailed: boolean;
  refreshWorkspaces: () => Promise<void>;
  switchWorkspace: (workspaceId: string) => Promise<void>;
}

const WorkspaceContext = createContext<WorkspaceContextType | undefined>(undefined);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { user, isLoading: authLoading, refetchUser } = useAuth();
  const router = useRouter();

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [switching, setSwitching] = useState(false);
  const switchingRef = useRef(false);
  const generation = useRef(0);
  const userId = user?.id;
  const userWorkspaceId = user?.current_workspace_id;

  // Read the current confirmed selection at call time. A late save in A can
  // still call the refresh function it captured before a switch to B.
  const activeScope = useRef<{ userId: string; workspaceId: string | null } | null>(null);
  const refreshWorkspaces = useCallback(async () => {
    const scope = activeScope.current;
    if (!scope) return;
    const selectedId = scope.workspaceId;
    const request = ++generation.current;
    setLoading(true);
    setLoadFailed(false);
    try {
      const available = await listWorkspaces();
      if (generation.current !== request) return;
      setWorkspaces(available);
      setCurrentWorkspace(selectedId
        ? available.find(w => w.id === selectedId) ?? null
        : null);
    } catch {
      if (generation.current !== request) return;
      setWorkspaces([]);
      setCurrentWorkspace(null);
      setLoadFailed(true);
    } finally {
      if (generation.current === request) setLoading(false);
    }
  }, []);

  const switchWorkspace = async (workspaceId: string) => {
    if (switchingRef.current) return;
    switchingRef.current = true;
    setSwitching(true);
    const previous = activeScope.current;
    let switchAccepted = false;
    activeScope.current = null;
    ++generation.current; // Invalidate an older list response before sending the switch.
    try {
      await switchWorkspaceApi(workspaceId);
      switchAccepted = true;
      const confirmed = await refetchUser();
      if (!confirmed || confirmed.id !== userId || confirmed.current_workspace_id !== workspaceId) {
        throw new Error("Could not verify the selected workspace");
      }
      activeScope.current = { userId: confirmed.id, workspaceId };
      await refreshWorkspaces();
      router.refresh();
    } catch (error) {
      // Only the switch response itself can prove the server kept the old scope.
      if (!switchAccepted && previous?.workspaceId && error instanceof ApiError &&
          [403, 404, 410].includes(error.status)) {
        // Other tabs can change the account-wide selection while B is pending.
        const identity = await refetchUser().catch(() => undefined);
        if (identity?.id === previous.userId && identity.current_workspace_id === previous.workspaceId) {
          activeScope.current = previous;
          await refreshWorkspaces();
          throw error;
        }
      }
      activeScope.current = null;
      // A lost response is ambiguous: the server may already have switched.
      ++generation.current;
      setCurrentWorkspace(null);
      setLoadFailed(true);
      setLoading(false);
      throw error;
    } finally {
      switchingRef.current = false;
      setSwitching(false);
    }
  };

  // Reload only when the values loadWorkspaces actually reads change. Depending
  // on the `user` object itself re-fires on every refetchUser() — it always
  // hands back a fresh object — which flips `loading` and makes WorkspaceGuard
  // unmount the whole authenticated subtree (#1532).
  useEffect(() => {
    if (switchingRef.current) return;
    const scope = userId && !authLoading ? { userId, workspaceId: userWorkspaceId ?? null } : null;
    activeScope.current = scope;
    void refreshWorkspaces();
    return () => {
      // A switch can publish its confirmed scope before this effect is cleaned up.
      if (activeScope.current === scope) {
        activeScope.current = null;
        ++generation.current;
      }
    };
  }, [userId, userWorkspaceId, authLoading, refreshWorkspaces]);

  const value: WorkspaceContextType = {
    currentWorkspace,
    currentWorkspaceId: currentWorkspace?.id || null,
    workspaces,
    loading: loading || switching,
    loadFailed,
    refreshWorkspaces,
    switchWorkspace,
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
