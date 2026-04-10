"use client";

/**
 * Context Switcher Component
 *
 * Dropdown component for switching between contexts.
 * Displays in the header and allows users to quickly change their active context.
 *
 * Issue #82: Context-based Multi-Collection Support
 * Issue #223: i18n support
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslations } from "next-intl";
import { useRouter, usePathname } from "next/navigation";
import { ChevronDown, Brain, Plus, Loader2 } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { cn, colors, transitions } from "@/styles/design-tokens";
import { getContexts } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { useSearchParams } from "next/navigation";

interface ContextSelectorProps {
  className?: string;
}

export function ContextSelector({ className }: ContextSelectorProps) {
  const t = useTranslations("contextSwitcher");

  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const contextIdFromUrl = searchParams?.get("context");

  const { refresh: refreshContextContext } = useMemoryContext();
  const [contexts, setContexts] = useState<Context[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchContexts = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await getContexts();
      setContexts(response.contexts);
      // Issue #246: No is_current field, context comes from URL
    } catch (err) {
      console.error("Failed to fetch contexts:", err);
      setError(t("failedToLoad"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    fetchContexts();
  }, [fetchContexts]);

  const handleSwitchContext = (context: Context) => {
    // Issue #246: Navigate to context stats page
    router.push(`/workspace/contexts/${context.id}`);
  };

  const handleManageContexts = () => {
    router.push("/workspace/contexts");
  };

  // Issue #246: Get context from URL parameter
  // Priority: ?context= param > /workspace/contexts/{uuid} path
  const pathContextId = pathname.match(/\/workspace\/contexts\/([^\/]+)/)?.[1];
  const effectiveContextId = contextIdFromUrl || pathContextId;

  const displayContext = effectiveContextId
    ? contexts.find((c) => c.id === effectiveContextId)
    : contexts[0]; // Default to first context

  if (loading) {
    return (
      <Button variant="ghost" size="sm" disabled className={className}>
        <Loader2 className="h-4 w-4 animate-spin mr-2" />
        {t("loading")}
      </Button>
    );
  }

  if (error) {
    return (
      <div className={cn("flex flex-col gap-1", className)}>
        <span className="text-sm text-red-600 dark:text-red-400 font-medium">
          {t("failedToLoad")}
        </span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-red-500">{error}</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={fetchContexts}
            className="text-red-600 hover:text-red-700 h-7"
          >
            <Brain className="h-3 w-3 mr-1" />
            {t("retry")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="default"
          className={cn(
            "flex items-center gap-2 h-10",
            transitions.default,
            className,
          )}
          disabled={loading}
        >
          {loading ? (
            <Loader2 className="h-5 w-5 animate-spin" />
          ) : (
            <Brain className="h-5 w-5" />
          )}
          <span className="max-w-[150px] truncate font-medium">
            {displayContext?.display_name ||
              displayContext?.name ||
              t("noContext")}
          </span>
          <ChevronDown className="h-4 w-4 opacity-50" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <DropdownMenuLabel>コンテキストを選択</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {contexts.length === 0 ? (
          <DropdownMenuItem disabled>
            {t("noContextsAvailable")}
          </DropdownMenuItem>
        ) : (
          contexts.map((context) => (
            <DropdownMenuItem
              key={context.id}
              onClick={() => handleSwitchContext(context)}
              className={cn(
                "flex items-center justify-between",
                context.id === effectiveContextId &&
                  "bg-slate-100 dark:bg-slate-800",
              )}
            >
              <div className="flex flex-col">
                <div className="flex items-center gap-1.5">
                  {context.is_public ? (
                    <span className="text-xs" title={t("public")}>
                      🌍
                    </span>
                  ) : context.is_private ? (
                    <span className="text-xs" title={t("private")}>
                      🔒
                    </span>
                  ) : (
                    <span className="text-xs" title={t("shared")}>
                      👥
                    </span>
                  )}
                  <span className="font-medium">
                    {context.display_name || context.name}
                  </span>
                </div>
                {context.is_default && (
                  <span className="text-xs text-slate-500">{t("default")}</span>
                )}
              </div>
              {context.id === effectiveContextId && (
                <span className="text-xs font-medium text-brand-green-600">
                  {t("current")}
                </span>
              )}
            </DropdownMenuItem>
          ))
        )}

        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={handleManageContexts}>
          <Plus className="h-4 w-4 mr-2" />
          {t("manageContexts")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
