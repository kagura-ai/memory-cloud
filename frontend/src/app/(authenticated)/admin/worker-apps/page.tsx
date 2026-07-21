"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Puzzle } from "lucide-react";

import { ErrorBanner } from "@/components/common/ErrorBanner";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { TableLoadingState } from "@/components/common/LoadingState";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/contexts/AuthContext";
import { useToast } from "@/hooks/use-toast";
import {
  createWorkerApp,
  listWorkerApps,
  rotateWorkerAppSecret,
  updateWorkerApp,
  type WorkerAppIdentity,
} from "@/lib/api/worker-apps";

export default function WorkerAppsPage() {
  const t = useTranslations("workerApps");
  const { user, isLoading: authLoading } = useAuth();
  const allowed = user?.role === "admin";
  const { toast } = useToast();
  const [apps, setApps] = useState<WorkerAppIdentity[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [appKey, setAppKey] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [names, setNames] = useState<Record<string, string>>({});
  const [rotationSecrets, setRotationSecrets] = useState<
    Record<string, string>
  >({});

  const reload = useCallback(async () => {
    try {
      setLoadError(null);
      const rows = await listWorkerApps();
      setApps(rows);
      // #1360: `names` holds only the user's unsaved drafts (the inputs
      // fall back to app.display_name). Reload must NOT reseed it with
      // server values — that silently discarded edits in other rows
      // whenever any action completed. Only prune keys that no longer
      // exist.
      setNames((current) => {
        const live = new Set(
          rows.map((app) => `${app.platform}:${app.app_key}`),
        );
        return Object.fromEntries(
          Object.entries(current).filter(([key]) => live.has(key)),
        );
      });
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  useEffect(() => {
    if (allowed) void reload();
  }, [allowed, reload]);

  const run = useCallback(
    async (key: string, action: () => Promise<unknown>): Promise<boolean> => {
      setBusyKey(key);
      try {
        await action();
        await reload();
        return true;
      } catch (error) {
        // #1360: user-action failures go to a destructive toast (error
        // surface rule) — inline banners are for load failures only.
        toast({
          variant: "destructive",
          title: t("operationFailed"),
          description: error instanceof Error ? error.message : String(error),
        });
        return false;
      } finally {
        setBusyKey(null);
      }
    },
    [reload, toast, t],
  );

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault();
    await run("create", async () => {
      await createWorkerApp({
        platform: "slack",
        app_key: appKey,
        display_name: displayName,
        signing_secret: signingSecret,
      });
      setAppKey("");
      setDisplayName("");
      setSigningSecret("");
    });
  };

  if (authLoading) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("description")} />
        <TableLoadingState rows={3} />
      </PageContainer>
    );
  }

  if (!allowed) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("description")} />
        <ErrorBanner error={t("forbidden")} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <Alert className="mb-4">
        <AlertDescription>{t("purposeNotice")}</AlertDescription>
      </Alert>

      <Alert className="mb-6">
        <AlertDescription>{t("secretNotice")}</AlertDescription>
      </Alert>

      <form
        onSubmit={handleCreate}
        className="mb-8 grid gap-3 rounded-md border p-4 md:grid-cols-4"
      >
        <Input
          aria-label={t("appKey")}
          placeholder={t("appKeyPlaceholder")}
          pattern="[a-z0-9][a-z0-9_-]{0,63}"
          value={appKey}
          onChange={(event) => setAppKey(event.target.value)}
          required
        />
        <Input
          aria-label={t("displayName")}
          placeholder={t("displayName")}
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          required
        />
        <Input
          aria-label={t("signingSecret")}
          placeholder={t("signingSecret")}
          type="password"
          autoComplete="new-password"
          value={signingSecret}
          onChange={(event) => setSigningSecret(event.target.value)}
          required
        />
        <Button type="submit" disabled={busyKey !== null}>
          {t("create")}
        </Button>
        <p className="text-sm text-muted-foreground md:col-span-4">
          {t("appKeyHelp")}
        </p>
        <p className="text-sm text-muted-foreground md:col-span-4">
          {t("signingSecretHelp")}
        </p>
      </form>

      {loadError ? (
        <ErrorBanner error={loadError} />
      ) : apps === null ? (
        <TableLoadingState rows={3} />
      ) : apps.length === 0 ? (
        <EmptyState
          icon={Puzzle}
          title={t("emptyTitle")}
          description={t("emptyDesc")}
        />
      ) : (
        <ul className="space-y-4">
          {apps.map((app) => {
            const key = `${app.platform}:${app.app_key}`;
            const busy = busyKey === key;
            return (
              <li key={key} className="rounded-md border p-4">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="font-mono font-medium">{app.app_key}</p>
                    <p className="text-xs text-muted-foreground">
                      {t("revision", { revision: app.revision })}
                    </p>
                  </div>
                  <span className="rounded-full border px-2 py-1 text-xs">
                    {t(`status_${app.status}`)}
                  </span>
                </div>

                <div className="grid gap-3 lg:grid-cols-2">
                  <div className="flex gap-2">
                    <Input
                      aria-label={t("displayNameFor", { appKey: app.app_key })}
                      value={names[key] ?? app.display_name}
                      onChange={(event) =>
                        setNames((current) => ({
                          ...current,
                          [key]: event.target.value,
                        }))
                      }
                    />
                    <Button
                      type="button"
                      variant="outline"
                      disabled={busy || busyKey !== null}
                      onClick={() =>
                        void run(key, () =>
                          updateWorkerApp(app, {
                            display_name: names[key] ?? app.display_name,
                          }),
                        ).then((saved) => {
                          // Drop this row's draft only AFTER the reload
                          // inside run() delivered the fresh server value
                          // — clearing earlier flashes the stale name
                          // mid-refetch, and a failed save keeps the
                          // draft for retry.
                          if (saved) {
                            setNames((current) => {
                              const { [key]: _saved, ...rest } = current;
                              return rest;
                            });
                          }
                        })
                      }
                    >
                      {t("save")}
                    </Button>
                  </div>

                  <div className="flex gap-2">
                    <Input
                      aria-label={t("newSigningSecretFor", {
                        appKey: app.app_key,
                      })}
                      type="password"
                      autoComplete="new-password"
                      placeholder={t("newSigningSecret")}
                      value={rotationSecrets[key] ?? ""}
                      onChange={(event) =>
                        setRotationSecrets((current) => ({
                          ...current,
                          [key]: event.target.value,
                        }))
                      }
                    />
                    <Button
                      type="button"
                      variant="outline"
                      disabled={
                        busy ||
                        busyKey !== null ||
                        !(rotationSecrets[key] ?? "")
                      }
                      onClick={() =>
                        void run(key, async () => {
                          await rotateWorkerAppSecret(
                            app,
                            rotationSecrets[key] ?? "",
                          );
                          setRotationSecrets((current) => ({
                            ...current,
                            [key]: "",
                          }));
                        })
                      }
                    >
                      {t("rotate")}
                    </Button>
                  </div>
                </div>

                <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
                  <span>
                    {t("secretRevision", {
                      revision: app.active_secret_revision ?? "—",
                    })}
                  </span>
                  {/* Rotation stages secret material without changing status,
                      so an unconfigured app with a staged secret needs the
                      explicit enable control (the backend rejects enabling
                      without a configured secret). */}
                  {(app.status !== "unconfigured" || app.has_active_secret) && (
                    <Button
                      type="button"
                      variant={
                        app.status === "active" ? "destructive" : "outline"
                      }
                      disabled={busy || busyKey !== null}
                      onClick={() =>
                        void run(key, () =>
                          updateWorkerApp(app, {
                            status:
                              app.status === "active" ? "disabled" : "active",
                          }),
                        )
                      }
                    >
                      {app.status === "active" ? t("disable") : t("enable")}
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </PageContainer>
  );
}
