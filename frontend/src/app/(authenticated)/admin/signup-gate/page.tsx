"use client";

/**
 * Admin Signup Gate Page
 *
 * Issue #358 Phase 1: admin-configurable signup gate.
 *
 * Two tabs:
 * - Settings: Enable toggle + Mode select (Sponsors/both disabled until Phase 2)
 * - Manual Allowlist: Add GitHub usernames (resolved to immutable numeric ID by
 *   the backend) and remove existing entries. The list is read from the server
 *   one page at a time (#2831); add and remove re-read the page.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight, Plus, Trash2 } from "lucide-react";

import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  LoadingState,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/hooks/use-toast";
import { useTabParam } from "@/hooks/useTabParam";
import { ApiError } from "@/lib/api/base";
import {
  addSignupAllowlistEntry,
  getSignupGateConfig,
  listSignupAllowlist,
  removeSignupAllowlistEntry,
  updateSignupGateConfig,
  type SignupAllowlistEntry,
  type SignupGateConfig,
  type SignupGateMode,
  type SignupGateProvider,
} from "@/lib/api/signup-gate";

const TABS = ["settings", "allowlist"] as const;

// Rows per allowlist page (the list route's default, #2831).
const PAGE_SIZE = 50;

export default function AdminSignupGatePage() {
  const t = useTranslations("admin.signupGate");
  const tCommon = useTranslations("admin.common");
  const { toast } = useToast();
  const [tab, setTab] = useTabParam("settings", "tab", TABS);

  const [config, setConfig] = useState<SignupGateConfig | null>(null);
  // One page of the allowlist (#2831): `total` counts every entry, `offset`
  // is where the shown page starts.
  const [allowlist, setAllowlist] = useState<SignupAllowlistEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // A page change or a re-read after add / remove (the first load is `loading`).
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [provider, setProvider] = useState<SignupGateProvider>("github");
  // Single input bound to both username and email — the placeholder /
  // aria-label switches based on the selected provider. Backend's
  // canonical payload is `{provider, github_username}` for github or
  // `{provider, email}` for google, so we route at submit time.
  const [username, setUsername] = useState("");
  const [adding, setAdding] = useState(false);
  // Guards against rapid toggle/select interleaving: without it, response
  // order is not response order and the UI can end up stale relative to the
  // last user action. Disabling the controls while a save is in flight
  // serializes the user's intent into one-at-a-time semantics.
  const [savingConfig, setSavingConfig] = useState(false);

  useEffect(() => {
    void loadAll();
  }, []);

  async function loadAll(): Promise<void> {
    setLoading(true);
    setLoadError(null);
    try {
      const [cfg, list] = await Promise.all([
        getSignupGateConfig(),
        listSignupAllowlist({ limit: PAGE_SIZE, offset: 0 }),
      ]);
      setConfig(cfg);
      setAllowlist(list.items);
      setTotal(list.total);
      setOffset(0);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function loadPage(nextOffset: number): Promise<void> {
    setListLoading(true);
    setListError(null);
    try {
      let shown = nextOffset;
      let list = await listSignupAllowlist({ limit: PAGE_SIZE, offset: shown });
      // The page emptied (its last row was removed): show the last page
      // that still has rows.
      if (list.items.length === 0 && list.total > 0 && shown > 0) {
        shown = Math.floor((list.total - 1) / PAGE_SIZE) * PAGE_SIZE;
        list = await listSignupAllowlist({ limit: PAGE_SIZE, offset: shown });
      }
      setAllowlist(list.items);
      setTotal(list.total);
      setOffset(shown);
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err));
    } finally {
      setListLoading(false);
    }
  }

  async function saveConfig(next: SignupGateConfig): Promise<void> {
    if (savingConfig) return;
    const previous = config;
    // Optimistic: reflect the user's toggle/select immediately so the
    // control doesn't appear to "snap back" during the PUT round-trip.
    // Roll back to `previous` on error.
    setConfig(next);
    setSavingConfig(true);
    try {
      const updated = await updateSignupGateConfig({
        enabled: next.enabled,
        mode: next.mode,
      });
      setConfig(updated);
      toast({ title: tCommon("success"), description: t("configSaved") });
    } catch (err) {
      setConfig(previous);
      const detail =
        err instanceof ApiError ? err.message : t("configSaveError");
      toast({
        title: tCommon("error"),
        description: detail,
        variant: "destructive",
      });
    } finally {
      setSavingConfig(false);
    }
  }

  async function handleAdd(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    const trimmed = username.trim();
    if (!trimmed) return;
    setAdding(true);
    try {
      // Issue #655: route the payload by selected provider. Backend
      // validates shape (rejects email when provider=github and vice
      // versa) so the UI just trusts the user's selection.
      await addSignupAllowlistEntry(
        provider === "github"
          ? { provider: "github", github_username: trimmed }
          : { provider: "google", email: trimmed },
      );
      setUsername("");
      toast({ title: tCommon("success"), description: t("addSuccess") });
      // The new entry is the newest: it leads the first page.
      await loadPage(0);
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : t("addError");
      toast({
        title: tCommon("error"),
        description: detail,
        variant: "destructive",
      });
    } finally {
      setAdding(false);
    }
  }

  async function handleRemove(entry: SignupAllowlistEntry): Promise<void> {
    if (
      !window.confirm(
        t("removeConfirm", {
          // subject_label is the provider-agnostic display string (github_username
          // for github, email for google); falls back to the legacy github_username
          // for any pre-#655 rows that somehow miss the new field.
          username: entry.subject_label || entry.github_username,
        }),
      )
    ) {
      return;
    }
    try {
      await removeSignupAllowlistEntry(entry.id);
      toast({ title: tCommon("success"), description: t("removeSuccess") });
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : t("removeError");
      toast({
        title: tCommon("error"),
        description: detail,
        variant: "destructive",
      });
      return;
    }
    await loadPage(offset);
  }

  const hasPrevious = offset > 0;
  const hasNext = offset + allowlist.length < total;

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      {loadError ? (
        <ErrorBanner error={loadError} />
      ) : loading || config === null ? (
        <LoadingState lines={6} />
      ) : (
        <Tabs value={tab} onValueChange={setTab} className="mt-4">
          <TabsList>
            <TabsTrigger value="settings">{t("tabSettings")}</TabsTrigger>
            <TabsTrigger value="allowlist">{t("tabAllowlist")}</TabsTrigger>
          </TabsList>

          <TabsContent value="settings" className="space-y-6 pt-6">
            <div className="flex items-start justify-between gap-4 max-w-2xl">
              <div className="space-y-1">
                <Label htmlFor="signup-gate-enabled" className="text-base">
                  {t("enabledToggle")}
                </Label>
                <p className="text-sm text-muted-foreground">
                  {t("enabledHelp")}
                </p>
              </div>
              <Switch
                id="signup-gate-enabled"
                checked={config.enabled}
                disabled={savingConfig}
                onCheckedChange={(checked) =>
                  void saveConfig({ ...config, enabled: checked })
                }
              />
            </div>

            <div className="space-y-2 max-w-sm">
              <Label htmlFor="signup-gate-mode" className="text-base">
                {t("modeLabel")}
              </Label>
              <Select
                value={config.mode}
                disabled={savingConfig}
                onValueChange={(mode) =>
                  void saveConfig({ ...config, mode: mode as SignupGateMode })
                }
              >
                <SelectTrigger id="signup-gate-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">{t("modeManual")}</SelectItem>
                  <SelectItem value="github_sponsors" disabled>
                    {t("modeGithubSponsorsPhase2")}
                  </SelectItem>
                  <SelectItem value="both" disabled>
                    {t("modeBothPhase2")}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </TabsContent>

          <TabsContent value="allowlist" className="space-y-4 pt-6">
            <form onSubmit={handleAdd} className="flex gap-2 max-w-xl">
              <Select
                value={provider}
                disabled={adding}
                onValueChange={(value) =>
                  setProvider(value as SignupGateProvider)
                }
              >
                <SelectTrigger className="w-32" aria-label={t("providerLabel")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="github">{t("providerGitHub")}</SelectItem>
                  <SelectItem value="google">{t("providerGoogle")}</SelectItem>
                </SelectContent>
              </Select>
              <Input
                placeholder={
                  provider === "github" ? t("addUsername") : t("addEmail")
                }
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={adding}
                aria-label={
                  provider === "github" ? t("addUsername") : t("addEmail")
                }
                type={provider === "google" ? "email" : "text"}
              />
              <Button type="submit" disabled={adding || !username.trim()}>
                <Plus className="h-4 w-4 mr-1" />
                {t("addButton")}
              </Button>
            </form>

            {listError ? (
              <ErrorBanner error={listError} />
            ) : listLoading ? (
              <TableLoadingState rows={5} />
            ) : allowlist.length === 0 ? (
              <EmptyState
                title={t("allowlistEmpty")}
                description={t("allowlistEmptyHint")}
              />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("tableProvider")}</TableHead>
                    <TableHead>{t("tableUser")}</TableHead>
                    <TableHead>{t("tableSource")}</TableHead>
                    <TableHead>{t("tableState")}</TableHead>
                    <TableHead className="w-[80px] text-right">
                      {t("tableActions")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {allowlist.map((entry) => {
                    // Display label: subject_label is canonical post-#655;
                    // fall back to github_username for any row missing it.
                    const label = entry.subject_label || entry.github_username;
                    // #1734: an email sign-up row has its own label.
                    const providerLabel =
                      entry.provider === "google"
                        ? t("providerGoogle")
                        : entry.provider === "email"
                          ? t("providerEmail")
                          : t("providerGitHub");
                    return (
                      <TableRow key={entry.id}>
                        <TableCell>{providerLabel}</TableCell>
                        <TableCell className="font-medium">
                          {label}
                          {entry.provider === "github" &&
                            entry.github_user_id && (
                              <span className="ml-2 text-xs text-muted-foreground">
                                {t("githubUserId", {
                                  id: entry.github_user_id,
                                })}
                              </span>
                            )}
                        </TableCell>
                        <TableCell>{t(`source.${entry.source}`)}</TableCell>
                        <TableCell>{t(`state.${entry.state}`)}</TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => void handleRemove(entry)}
                            aria-label={t("removeAllowlistEntry", {
                              username: label,
                            })}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}

            {total > 0 && (
              <div className="flex items-center justify-between">
                <div className="text-sm text-muted-foreground">
                  {t("pagination.showing", {
                    from: offset + 1,
                    to: offset + allowlist.length,
                    total,
                  })}
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!hasPrevious || listLoading}
                    onClick={() =>
                      void loadPage(Math.max(0, offset - PAGE_SIZE))
                    }
                  >
                    <ChevronLeft className="h-4 w-4 mr-1" />
                    {t("pagination.previous")}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!hasNext || listLoading}
                    onClick={() => void loadPage(offset + PAGE_SIZE)}
                  >
                    {t("pagination.next")}
                    <ChevronRight className="h-4 w-4 ml-1" />
                  </Button>
                </div>
              </div>
            )}
          </TabsContent>
        </Tabs>
      )}
    </PageContainer>
  );
}
