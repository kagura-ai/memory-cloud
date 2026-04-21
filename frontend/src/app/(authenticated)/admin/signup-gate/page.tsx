"use client";

/**
 * Admin Signup Gate Page
 *
 * Issue #358 Phase 1: admin-configurable signup gate.
 *
 * Two tabs:
 * - Settings: Enable toggle + Mode select (Sponsors/both disabled until Phase 2)
 * - Manual Allowlist: Add GitHub usernames (resolved to immutable numeric ID by
 *   the backend) and remove existing entries.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Plus, Trash2 } from "lucide-react";

import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";
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
} from "@/lib/api/signup-gate";

const TABS = ["settings", "allowlist"] as const;

export default function AdminSignupGatePage() {
  const t = useTranslations("admin.signupGate");
  const tCommon = useTranslations("admin.common");
  const { toast } = useToast();
  const [tab, setTab] = useTabParam("settings", "tab", TABS);

  const [config, setConfig] = useState<SignupGateConfig | null>(null);
  const [allowlist, setAllowlist] = useState<SignupAllowlistEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

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
        listSignupAllowlist(),
      ]);
      setConfig(cfg);
      setAllowlist(list);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function saveConfig(next: SignupGateConfig): Promise<void> {
    if (savingConfig) return;
    setSavingConfig(true);
    try {
      const updated = await updateSignupGateConfig({
        enabled: next.enabled,
        mode: next.mode,
      });
      setConfig(updated);
      toast({ title: tCommon("success"), description: t("configSaved") });
    } catch (err) {
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
      const entry = await addSignupAllowlistEntry(trimmed);
      setAllowlist((prev) => [entry, ...prev]);
      setUsername("");
      toast({ title: tCommon("success"), description: t("addSuccess") });
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
      !window.confirm(t("removeConfirm", { username: entry.github_username }))
    ) {
      return;
    }
    try {
      await removeSignupAllowlistEntry(entry.id);
      setAllowlist((prev) => prev.filter((e) => e.id !== entry.id));
      toast({ title: tCommon("success"), description: t("removeSuccess") });
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : t("removeError");
      toast({
        title: tCommon("error"),
        description: detail,
        variant: "destructive",
      });
    }
  }

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
            <form onSubmit={handleAdd} className="flex gap-2 max-w-md">
              <Input
                placeholder={t("addUsername")}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={adding}
                aria-label={t("addUsername")}
              />
              <Button type="submit" disabled={adding || !username.trim()}>
                <Plus className="h-4 w-4 mr-1" />
                {t("addButton")}
              </Button>
            </form>

            {allowlist.length === 0 ? (
              <EmptyState
                title={t("allowlistEmpty")}
                description={t("allowlistEmptyHint")}
              />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("tableUser")}</TableHead>
                    <TableHead>{t("tableSource")}</TableHead>
                    <TableHead>{t("tableState")}</TableHead>
                    <TableHead className="w-[80px] text-right">
                      {t("tableActions")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {allowlist.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell className="font-medium">
                        {entry.github_username}
                        <span className="ml-2 text-xs text-muted-foreground">
                          {t("githubUserId", { id: entry.github_user_id })}
                        </span>
                      </TableCell>
                      <TableCell>{t(`source.${entry.source}`)}</TableCell>
                      <TableCell>{t(`state.${entry.state}`)}</TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => void handleRemove(entry)}
                          aria-label={t("removeAllowlistEntry", {
                            username: entry.github_username,
                          })}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </TabsContent>
        </Tabs>
      )}
    </PageContainer>
  );
}
