/**
 * ConnectedAppsSection (#2129)
 *
 * The OAuth apps the signed-in user has authorized (Claude, ChatGPT, Cursor,
 * custom clients…), from `GET /api/v1/me/oauth/grants`, each with its MCP
 * default context: a context-scoped tool called over that app's token
 * without `context_id` runs in it (`?context=` on the URL wins). Set with
 * `PUT …/default-context`, cleared with `DELETE`.
 *
 * Grants are per user, not per workspace, so the list does not change with
 * the workspace switcher; the contexts offered are the current workspace's
 * (a default elsewhere still shows, by name).
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Plug } from "lucide-react";
import { Section } from "@/components/common/Section";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { getContexts } from "@/lib/api/contexts";
import {
  clearOAuthGrantDefaultContext,
  listOAuthGrants,
  setOAuthGrantDefaultContext,
} from "@/lib/api/oauth-grants";
import type { OAuthGrant } from "@/lib/types/oauth-grant";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";
import {
  DefaultContextControl,
  type DefaultContextOption,
} from "@/components/credentials/DefaultContextControl";

export function ConnectedAppsSection() {
  const t = useTranslations("connectedApps");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { user } = useAuth();
  const { currentWorkspaceId } = useWorkspace();
  const { toast } = useToast();

  const [grants, setGrants] = useState<OAuthGrant[] | null>(null);
  // The load failure: the server's message, or "" for none usable (shown
  // as the translated fallback). Kept raw so loadGrants does not depend on
  // the translator and re-fetch on every render.
  const [error, setError] = useState<string | null>(null);
  const [contexts, setContexts] = useState<DefaultContextOption[]>([]);

  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const loadGrants = useCallback(async () => {
    try {
      setError(null);
      const data = await listOAuthGrants();
      if (isMountedRef.current) setGrants(data);
    } catch (err: unknown) {
      if (isMountedRef.current) {
        setError(err instanceof Error ? err.message.trim() : "");
        setGrants([]);
      }
    }
  }, []);

  useEffect(() => {
    void loadGrants();
  }, [loadGrants]);

  // The contexts offered: the current workspace's. A failed fetch leaves
  // the list empty — "No default" and Clear still work.
  useEffect(() => {
    if (!currentWorkspaceId) return;
    let cancelled = false;
    getContexts()
      .then((resp) => {
        if (cancelled || !isMountedRef.current) return;
        setContexts(
          (resp?.contexts ?? []).map((c) => ({
            id: c.id,
            label: c.display_name || c.name,
          })),
        );
      })
      .catch(() => {
        if (!cancelled && isMountedRef.current) setContexts([]);
      });
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId]);

  // User-action failures → toast; rethrown so the control keeps the choice.
  const saveDefault = async (grant: OAuthGrant, contextId: string | null) => {
    try {
      let updated: OAuthGrant;
      if (contextId) {
        updated = await setOAuthGrantDefaultContext(grant.client_id, contextId);
      } else {
        await clearOAuthGrantDefaultContext(grant.client_id);
        updated = {
          ...grant,
          default_context_id: null,
          default_context_name: null,
        };
      }
      setGrants((prev) =>
        (prev ?? []).map((g) => (g.client_id === grant.client_id ? updated : g)),
      );
      toast({
        title: tCommon("success"),
        description: contextId ? t("saved") : t("cleared"),
      });
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
      throw err;
    }
  };

  return (
    <Section title={t("title")} description={t("description")}>
      <ErrorBanner error={error === null ? null : error || t("loadError")} />
      {grants === null ? (
        <TableLoadingState rows={2} />
      ) : grants.length === 0 ? (
        error ? null : (
          <EmptyState
            icon={Plug}
            title={t("emptyTitle")}
            description={t("emptyDescription")}
          />
        )
      ) : (
        <ul
          className="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg"
          aria-label={t("title")}
        >
          {grants.map((grant) => (
            <li
              key={grant.client_id}
              className="flex flex-col gap-3 p-4 md:flex-row md:items-center md:justify-between"
            >
              <div className="space-y-1 min-w-0">
                <p className="font-semibold text-gray-900 dark:text-gray-100 break-words">
                  {grant.client_name}
                </p>
                <div className="flex flex-wrap items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
                  <span>{t("scopes")}:</span>
                  {grant.scopes.length > 0 ? (
                    grant.scopes.map((scope) => (
                      <code
                        key={scope}
                        className="bg-gray-100 dark:bg-gray-900 px-1.5 py-0.5 rounded"
                      >
                        {scope}
                      </code>
                    ))
                  ) : (
                    <span>—</span>
                  )}
                </div>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {t("lastAuthorized")}:{" "}
                  {grant.last_issued_at ? (
                    <span
                      title={formatDateTime(
                        grant.last_issued_at,
                        user?.timezone,
                        locale,
                      )}
                    >
                      {formatRelativeTime(grant.last_issued_at, locale)}
                    </span>
                  ) : (
                    <span>—</span>
                  )}
                </p>
              </div>
              <div className="space-y-1 md:text-right">
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {t("defaultContext")}
                </p>
                <DefaultContextControl
                  currentId={grant.default_context_id}
                  currentName={grant.default_context_name}
                  options={contexts}
                  onSave={(contextId) => saveDefault(grant, contextId)}
                  ariaLabel={t("defaultContextFor", { name: grant.client_name })}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
