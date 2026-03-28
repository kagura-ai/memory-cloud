'use client';

/**
 * Context Context
 *
 * Provides global context state across the application.
 * When context switches, all consuming components automatically re-render.
 *
 * Issue #82: Context-based Multi-Collection Support
 */

import { createContext, useContext, useEffect, useState, useCallback, useMemo, ReactNode } from 'react';
import { getContext } from '@/lib/api/contexts';
import type { Context } from '@/lib/types/context';
import { useSearchParams } from 'next/navigation';

interface MemoryContextContextValue {
  currentContext: Context | null;
  contextId: string | null;
  contextName: string | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

const MemoryContextContext = createContext<MemoryContextContextValue | undefined>(undefined);

export function MemoryContextProvider({ children }: { children: ReactNode }) {
  const searchParams = useSearchParams();
  const contextIdFromUrl = searchParams?.get('context');

  const [currentContext, setCurrentContext] = useState<Context | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setIsLoading(true);
      setError(null);

      // Issue #246: Get context from URL parameter instead of "current" API
      if (contextIdFromUrl) {
        const context = await getContext(contextIdFromUrl);
        setCurrentContext(context);
      } else {
        // No context selected - set to null
        setCurrentContext(null);
      }
    } catch (err: any) {
      // Silent fail if no context or not found
      if (err?.status === 404 || err?.status === 400) {
        setCurrentContext(null);
        setError(null);
      } else {
        console.error('Failed to fetch context:', err);
        setError('Failed to load context');
        setCurrentContext(null);
      }
    } finally {
      setIsLoading(false);
    }
  }, [contextIdFromUrl]);

  useEffect(() => {
    // Load context when URL parameter changes
    refresh();
  }, [refresh]);

  const value: MemoryContextContextValue = useMemo(
    () => ({
      currentContext,
      contextId: currentContext?.id || null,
      contextName: currentContext?.display_name || currentContext?.name || null,
      isLoading,
      error,
      refresh,
    }),
    [currentContext, isLoading, error, refresh]
  );

  return <MemoryContextContext.Provider value={value}>{children}</MemoryContextContext.Provider>;
}

export function useMemoryContext(): MemoryContextContextValue {
  const context = useContext(MemoryContextContext);
  if (context === undefined) {
    throw new Error('useMemoryContext must be used within MemoryContextProvider');
  }
  return context;
}
