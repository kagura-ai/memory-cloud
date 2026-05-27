"use client";

/**
 * User Detail Page
 *
 * Issue #164: User Management拡張 - User detail page
 *
 * Shows comprehensive user information including workspaces, contexts, and stats.
 */

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { LoadingState, InlineSpinner } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ArrowLeft,
  Building2,
  FolderOpen,
  BarChart2,
  Shield,
  Star,
  CreditCard,
  Layers,
  Minus,
  Plus,
  AlertTriangle,
} from "lucide-react";
import { apiClient } from "@/lib/api";
import {
  updateWorkspaceSlotBonus,
  type WorkspaceSummary,
} from "@/lib/api/admin";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { useToast } from "@/hooks/use-toast";
import { USER_DETAIL_TEST_IDS } from "./testids";

interface UserDetail {
  user: {
    id: string;
    email: string;
    name: string;
    picture?: string;
    role: string;
    is_initial_admin?: boolean;
    created_at: string;
    last_login_at?: string;
  };
  workspaces: Array<{
    workspace_id: string;
    workspace_name: string;
    role: string;
    is_primary: boolean;
    joined_at?: string;
    plan_name?: string;
  }>;
  accessible_contexts: Array<{
    context_id: string;
    context_name: string;
    workspace_id: string;
    workspace_name: string;
    role: string;
    last_used_at?: string;
  }>;
  stats: {
    total_memories: number;
    working_memories: number;
    persistent_memories: number;
    active_api_keys: number;
  };
  workspace_summary?: WorkspaceSummary | null; // #676 (optional during rollout)
}

export default function UserDetailPage() {
  const params = useParams();
  const router = useRouter();
  const t = useTranslations("admin.users.detail");
  const tCommon = useTranslations("admin.common");
  const locale = useLocale();
  const userId = params.userId as string;
  const [userDetail, setUserDetail] = useState<UserDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [planDialog, setPlanDialog] = useState<{
    open: boolean;
    workspaceId: string | null;
    workspaceName: string | null;
    currentPlan: string | null;
  }>({
    open: false,
    workspaceId: null,
    workspaceName: null,
    currentPlan: null,
  });
  const [newPlan, setNewPlan] = useState<string>("");

  // Workspace slot bonus state (#676)
  // `bonusPending` is the in-flight delta (so we can show a spinner on the
  // pressed button only); `destructiveModal` defers the PATCH until the
  // admin types a reason for over-cap operations.
  const [bonusPending, setBonusPending] = useState<-1 | 1 | null>(null);
  const [destructiveModal, setDestructiveModal] = useState<{
    open: boolean;
    delta: number;
    reason: string;
    warnText: string;
    submitting: boolean;
  }>({ open: false, delta: -1, reason: "", warnText: "", submitting: false });

  const { toast } = useToast();

  useEffect(() => {
    loadUserDetail();
  }, [userId]);

  const loadUserDetail = async () => {
    try {
      setLoading(true);
      setLoadError(null);
      const data = await apiClient.get<UserDetail>(
        `/api/v1/admin/users/${userId}`,
      );
      setUserDetail(data);
    } catch (error: unknown) {
      // Page-level load failure → ErrorBanner (per .claude/rules/frontend.md
      // error-surface rule). No toast for the same event — one channel per
      // error class.
      const message =
        error instanceof Error ? error.message : t("messages.loadError");
      setLoadError(message);
    } finally {
      setLoading(false);
    }
  };

  const openPlanDialog = (workspace: UserDetail["workspaces"][0]) => {
    setPlanDialog({
      open: true,
      workspaceId: workspace.workspace_id,
      workspaceName: workspace.workspace_name,
      currentPlan: workspace.plan_name || "free",
    });
    setNewPlan(workspace.plan_name || "free");
  };

  const handleChangePlan = async () => {
    if (!planDialog.workspaceId || !newPlan) return;

    try {
      await apiClient.put(
        `/api/v1/admin/plans/workspaces/${planDialog.workspaceId}/plan`,
        {
          plan_name: newPlan,
          reason: "Changed by admin via user detail page",
        },
      );

      toast({
        title: tCommon("success"),
        description: t("messages.planChangeSuccessDesc", {
          plan: newPlan,
          workspace: planDialog.workspaceName ?? "",
        }),
      });

      setPlanDialog({
        open: false,
        workspaceId: null,
        workspaceName: null,
        currentPlan: null,
      });
      loadUserDetail();
    } catch (error: unknown) {
      const message =
        error instanceof Error
          ? error.message
          : t("messages.planChangeErrorDesc");
      toast({
        title: tCommon("error"),
        description: message,
        variant: "destructive",
      });
    }
  };

  // ---------- #676 workspace slot bonus handlers ----------

  /**
   * Send PATCH and reconcile workspace_summary with the returned state.
   *
   * Uses functional setUserDetail updates so a concurrent loadUserDetail()
   * (e.g. triggered by the change-plan flow) cannot have its result
   * clobbered by this section's reconcile, and so a rollback restores
   * only workspace_summary instead of overwriting the entire userDetail
   * with a stale snapshot.
   */
  const commitBonusDelta = async (delta: number, reason: string | null) => {
    if (!userDetail?.workspace_summary) return;
    const summarySnapshot = userDetail.workspace_summary;

    // Optimistic update — render the projected state immediately so the
    // [-]/[+] feels responsive. Reconciled with the authoritative value
    // returned by the PATCH below.
    const projectedBonus = summarySnapshot.workspace_slot_bonus + delta;
    const projectedCap = summarySnapshot.base_cap + projectedBonus;
    setUserDetail((prev) =>
      prev && prev.workspace_summary
        ? {
            ...prev,
            workspace_summary: {
              ...prev.workspace_summary,
              workspace_slot_bonus: projectedBonus,
              cap: projectedCap,
              is_at_cap: prev.workspace_summary.owned_count >= projectedCap,
            },
          }
        : prev,
    );
    setBonusPending(delta as -1 | 1);

    try {
      const response = await updateWorkspaceSlotBonus(userId, {
        delta,
        reason,
      });
      setUserDetail((prev) =>
        prev && prev.workspace_summary
          ? {
              ...prev,
              workspace_summary: {
                ...prev.workspace_summary,
                workspace_slot_bonus: response.after_value,
                owned_count: response.owned_count,
                base_cap: response.base_cap,
                cap: response.cap,
                is_at_cap: response.is_at_cap,
              },
            }
          : prev,
      );
      toast({
        title: t("messages.bonusUpdateSuccessTitle"),
        description: t("messages.bonusUpdateSuccessDesc", {
          before: response.before_value,
          after: response.after_value,
        }),
      });
    } catch (error: unknown) {
      // Refetch authoritative state on PATCH failure. A naive
      // ``setUserDetail(snapshot)`` rollback (or any rollback that
      // compares only ``workspace_slot_bonus``) cannot reliably
      // distinguish "my optimistic write is still in place" from
      // "another admin's update happened to land on the same projected
      // value" — both produce identical state but different rollback
      // intents. ``loadUserDetail()`` re-reads the server, so the
      // post-failure state always reflects truth. Extra round-trip on
      // the rare failure path is acceptable for admin-only endpoint.
      const message =
        error instanceof Error
          ? error.message
          : t("messages.bonusUpdateErrorDesc");
      toast({
        title: tCommon("error"),
        description: message,
        variant: "destructive",
      });
      void loadUserDetail();
    } finally {
      setBonusPending(null);
    }
  };

  /**
   * Entry point for the [+] and [-] buttons. Routes destructive (-) ops
   * through the reason modal; everything else commits immediately.
   */
  const handleBonusDelta = async (delta: 1 | -1) => {
    if (!userDetail?.workspace_summary || bonusPending !== null) return;
    const summary = userDetail.workspace_summary;
    const projectedBonus = summary.workspace_slot_bonus + delta;
    const projectedCap = summary.base_cap + projectedBonus;

    if (projectedBonus < 0) {
      toast({
        title: t("messages.bonusAt0Title"),
        description: t("messages.bonusAt0Desc"),
        variant: "destructive",
      });
      return;
    }

    const destructive = delta < 0 && projectedCap < summary.owned_count;
    if (destructive) {
      const shortfall = summary.owned_count - projectedCap;
      setDestructiveModal({
        open: true,
        delta,
        reason: "",
        warnText: t("messages.overCapWarn", {
          owned: summary.owned_count,
          projected: projectedBonus,
          cap: projectedCap,
          shortfall,
        }),
        submitting: false,
      });
      return;
    }

    await commitBonusDelta(delta, null);
  };

  const submitDestructive = async () => {
    const reason = destructiveModal.reason.trim();
    if (!reason) return; // Submit button is also disabled in this state
    setDestructiveModal({ ...destructiveModal, submitting: true });
    try {
      await commitBonusDelta(destructiveModal.delta, reason);
    } finally {
      setDestructiveModal({
        open: false,
        delta: -1,
        reason: "",
        warnText: "",
        submitting: false,
      });
    }
  };

  if (loadError) {
    return (
      <PageContainer>
        <PageHeader
          title={t("title")}
          description={t("descriptionError")}
          actions={
            <Button
              variant="outline"
              onClick={() => router.push("/admin/users")}
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              {t("backToUsers")}
            </Button>
          }
        />
        <ErrorBanner error={loadError} />
      </PageContainer>
    );
  }

  if (loading || !userDetail) {
    return (
      <PageContainer>
        <PageHeader
          title={t("titleLoading")}
          description={t("descriptionLoading")}
        />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        description={`${userDetail.user.name} (${userDetail.user.email})`}
        actions={
          <Button variant="outline" onClick={() => router.push("/admin/users")}>
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("backToUsers")}
          </Button>
        }
      />

      {/* User Info Card */}
      <Card>
        <CardHeader>
          <CardTitle>{t("userInfo.title")}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4 mb-6">
            {userDetail.user.picture ? (
              <img
                src={userDetail.user.picture}
                alt={userDetail.user.name}
                className="w-16 h-16 rounded-full"
              />
            ) : (
              <div className="w-16 h-16 rounded-full bg-gray-200 flex items-center justify-center">
                <Shield className="w-8 h-8 text-gray-500" />
              </div>
            )}
            <div>
              <h3 className="text-xl font-semibold">{userDetail.user.name}</h3>
              <p className="text-gray-500">{userDetail.user.email}</p>
              <div className="flex gap-2 mt-2">
                <Badge
                  variant={
                    userDetail.user.role === "admin" ? "destructive" : "default"
                  }
                >
                  {userDetail.user.role === "admin" && (
                    <Shield className="h-3 w-3 mr-1" />
                  )}
                  {userDetail.user.role}
                </Badge>
                {userDetail.user.is_initial_admin && (
                  <Badge
                    variant="secondary"
                    className="bg-amber-100 text-amber-800"
                  >
                    {t("userInfo.initialAdminBadge")}
                  </Badge>
                )}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="text-gray-500">{t("userInfo.created")}</p>
              <p className="font-medium">
                {formatRelativeTime(userDetail.user.created_at, locale)}
              </p>
            </div>
            <div>
              <p className="text-gray-500">{t("userInfo.lastLogin")}</p>
              <p className="font-medium">
                {userDetail.user.last_login_at
                  ? formatRelativeTime(userDetail.user.last_login_at, locale)
                  : t("userInfo.never")}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Workspaces Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Building2 className="h-5 w-5" />
            <CardTitle>
              {t("workspaces.title", {
                count: userDetail.workspaces.length,
              })}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {userDetail.workspaces.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("workspaces.table.workspace")}</TableHead>
                  <TableHead>{t("workspaces.table.role")}</TableHead>
                  <TableHead>{t("workspaces.table.plan")}</TableHead>
                  <TableHead>{t("workspaces.table.joined")}</TableHead>
                  <TableHead className="text-right">
                    {t("workspaces.table.actions")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {userDetail.workspaces.map((workspace) => (
                  <TableRow key={workspace.workspace_id}>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        {workspace.is_primary && (
                          <Star className="h-4 w-4 text-yellow-500" />
                        )}
                        <span className="font-medium">
                          {workspace.workspace_name}
                        </span>
                        {workspace.is_primary && (
                          <Badge variant="outline" className="text-xs">
                            {t("workspaces.primaryBadge")}
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge>{workspace.role}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          workspace.plan_name === "pro"
                            ? "destructive"
                            : workspace.plan_name === "basic"
                              ? "default"
                              : "secondary"
                        }
                      >
                        {workspace.plan_name || "free"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-gray-500">
                      {workspace.joined_at
                        ? formatRelativeTime(workspace.joined_at, locale)
                        : t("workspaces.notAvailable")}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => openPlanDialog(workspace)}
                      >
                        <CreditCard className="h-4 w-4 mr-2" />
                        {t("workspaces.changePlanButton")}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-gray-500 text-sm">{t("workspaces.empty")}</p>
          )}
        </CardContent>
      </Card>

      {/* Workspace Capacity Card (#676) */}
      {userDetail.workspace_summary && (
        <Card data-testid={USER_DETAIL_TEST_IDS.workspaceCapacitySection}>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Layers className="h-5 w-5" />
              <CardTitle>{t("workspaceCapacity.title")}</CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            {(() => {
              const summary = userDetail.workspace_summary;
              const usage =
                summary.cap > 0 ? summary.owned_count / summary.cap : 0;
              const badgeVariant: "destructive" | "secondary" | "outline" =
                usage >= 0.95
                  ? "destructive"
                  : usage >= 0.8
                    ? "secondary"
                    : "outline";
              const usagePct = Math.round(usage * 100);
              return (
                <div className="space-y-4">
                  <div className="flex items-baseline gap-3">
                    <p
                      className="text-sm"
                      data-testid={
                        USER_DETAIL_TEST_IDS.workspaceCapacityCapDisplay
                      }
                    >
                      <span className="font-medium">
                        {t("workspaceCapacity.ownedLabel", {
                          count: summary.owned_count,
                        })}
                      </span>{" "}
                      / {t("workspaceCapacity.capLabel", { cap: summary.cap })}
                      <span className="text-gray-500 ml-2">
                        {t("workspaceCapacity.formula", {
                          base: summary.base_cap,
                          bonus: summary.workspace_slot_bonus,
                        })}
                      </span>
                    </p>
                    <Badge variant={badgeVariant}>
                      {summary.is_at_cap
                        ? t("workspaceCapacity.atCapBadge")
                        : t("workspaceCapacity.usagePctBadge", {
                            pct: usagePct,
                          })}
                    </Badge>
                  </div>

                  <div className="flex items-center gap-3">
                    <span className="text-sm text-gray-500">
                      {t("workspaceCapacity.slotBonusLabel")}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={t("workspaceCapacity.decrementAria")}
                      data-testid={
                        USER_DETAIL_TEST_IDS.workspaceCapacityDecrement
                      }
                      onClick={() => handleBonusDelta(-1)}
                      disabled={
                        bonusPending !== null ||
                        summary.workspace_slot_bonus === 0
                      }
                    >
                      {bonusPending === -1 ? (
                        <InlineSpinner />
                      ) : (
                        <Minus className="h-4 w-4" />
                      )}
                    </Button>
                    <span
                      className="font-mono font-medium text-lg min-w-[2ch] text-center"
                      data-testid={
                        USER_DETAIL_TEST_IDS.workspaceCapacityBonusValue
                      }
                    >
                      {summary.workspace_slot_bonus}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={t("workspaceCapacity.incrementAria")}
                      data-testid={
                        USER_DETAIL_TEST_IDS.workspaceCapacityIncrement
                      }
                      onClick={() => handleBonusDelta(1)}
                      disabled={bonusPending !== null}
                    >
                      {bonusPending === 1 ? (
                        <InlineSpinner />
                      ) : (
                        <Plus className="h-4 w-4" />
                      )}
                    </Button>
                  </div>

                  {summary.owned_workspaces.length > 0 ? (
                    <div>
                      <p className="text-xs text-gray-500 mb-2">
                        {t("workspaceCapacity.ownedWorkspacesLabel", {
                          count: summary.owned_workspaces.length,
                        })}
                      </p>
                      <ul className="space-y-1">
                        {summary.owned_workspaces.map((ws) => (
                          <li
                            key={ws.id}
                            className="flex items-center justify-between text-sm"
                          >
                            <span className="font-medium">{ws.name}</span>
                            <Badge
                              variant={
                                ws.plan_name === "pro"
                                  ? "destructive"
                                  : ws.plan_name === "basic"
                                    ? "default"
                                    : "secondary"
                              }
                              className="text-xs"
                            >
                              {ws.plan_name}
                            </Badge>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <p className="text-xs text-gray-500">
                      {t("workspaceCapacity.noOwnedWorkspaces")}
                    </p>
                  )}
                </div>
              );
            })()}
          </CardContent>
        </Card>
      )}

      {/* Accessible Contexts Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <FolderOpen className="h-5 w-5" />
            <CardTitle>
              {t("accessibleContexts.title", {
                count: userDetail.accessible_contexts.length,
              })}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {userDetail.accessible_contexts.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("accessibleContexts.table.context")}</TableHead>
                  <TableHead>
                    {t("accessibleContexts.table.workspace")}
                  </TableHead>
                  <TableHead>{t("accessibleContexts.table.role")}</TableHead>
                  <TableHead>
                    {t("accessibleContexts.table.lastUsed")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {userDetail.accessible_contexts.map((ctx) => (
                  <TableRow key={ctx.context_id}>
                    <TableCell className="font-medium">
                      {ctx.context_name}
                    </TableCell>
                    <TableCell className="text-sm text-gray-500">
                      {ctx.workspace_name}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{ctx.role}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-gray-500">
                      {ctx.last_used_at
                        ? formatRelativeTime(ctx.last_used_at, locale)
                        : t("userInfo.never")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-gray-500 text-sm">
              {t("accessibleContexts.empty")}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Statistics Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <BarChart2 className="h-5 w-5" />
            <CardTitle>{t("stats.title")}</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-500">
                {t("stats.totalMemories")}
              </p>
              <p className="text-3xl font-bold">
                {userDetail.stats.total_memories}
              </p>
              <p className="text-xs text-gray-500 mt-1">
                {t("stats.memoryBreakdown", {
                  working: userDetail.stats.working_memories,
                  persistent: userDetail.stats.persistent_memories,
                })}
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-500">
                {t("stats.activeApiKeys")}
              </p>
              <p className="text-3xl font-bold">
                {userDetail.stats.active_api_keys}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Change Plan Dialog */}
      <Dialog
        open={planDialog.open}
        onOpenChange={(open) => setPlanDialog({ ...planDialog, open })}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("changePlanDialog.title")}</DialogTitle>
            <DialogDescription>
              {t("changePlanDialog.description", {
                workspaceName: planDialog.workspaceName ?? "",
              })}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div>
              <label className="text-sm font-medium">
                {t("changePlanDialog.currentPlanLabel")}
              </label>
              <div className="mt-2">
                <Badge
                  variant={
                    planDialog.currentPlan === "pro" ? "destructive" : "default"
                  }
                >
                  {planDialog.currentPlan}
                </Badge>
              </div>
            </div>

            <div>
              <label className="text-sm font-medium">
                {t("changePlanDialog.newPlanLabel")}
              </label>
              <Select value={newPlan} onValueChange={setNewPlan}>
                <SelectTrigger className="mt-2">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="free">
                    {t("changePlanDialog.planOptions.free")}
                  </SelectItem>
                  <SelectItem value="basic">
                    {t("changePlanDialog.planOptions.basic")}
                  </SelectItem>
                  <SelectItem value="pro">
                    {t("changePlanDialog.planOptions.pro")}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="bg-yellow-50 border border-yellow-200 rounded p-3">
              <p className="text-sm text-yellow-800">
                {t("changePlanDialog.warningText")}
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() =>
                setPlanDialog({
                  open: false,
                  workspaceId: null,
                  workspaceName: null,
                  currentPlan: null,
                })
              }
            >
              {tCommon("cancel")}
            </Button>
            <Button
              onClick={handleChangePlan}
              disabled={newPlan === planDialog.currentPlan}
            >
              {t("changePlanDialog.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Destructive slot bonus modal (#676) */}
      <Dialog
        open={destructiveModal.open}
        onOpenChange={(open) =>
          !destructiveModal.submitting &&
          setDestructiveModal({ ...destructiveModal, open })
        }
      >
        <DialogContent data-testid={USER_DETAIL_TEST_IDS.reasonModal}>
          <DialogHeader>
            <DialogTitle>{t("reasonModal.title")}</DialogTitle>
            <DialogDescription>
              {t("reasonModal.description")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-2">
            <div
              role="alert"
              className="flex gap-2 rounded border border-destructive/50 bg-destructive/10 p-3 text-sm"
            >
              <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5 text-destructive" />
              <p>{destructiveModal.warnText}</p>
            </div>

            <div>
              <label htmlFor="bonus-reason" className="text-sm font-medium">
                {t("reasonModal.reasonLabel")}
              </label>
              <Textarea
                id="bonus-reason"
                data-testid={USER_DETAIL_TEST_IDS.reasonModalInput}
                value={destructiveModal.reason}
                onChange={(e) =>
                  setDestructiveModal({
                    ...destructiveModal,
                    reason: e.target.value,
                  })
                }
                placeholder={t("reasonModal.reasonPlaceholder")}
                rows={3}
                maxLength={500}
                disabled={destructiveModal.submitting}
                className="mt-2"
              />
              <p className="text-xs text-gray-500 mt-1">
                {t("reasonModal.charCounter", {
                  count: destructiveModal.reason.length,
                })}
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() =>
                setDestructiveModal({
                  open: false,
                  delta: -1,
                  reason: "",
                  warnText: "",
                  submitting: false,
                })
              }
              disabled={destructiveModal.submitting}
            >
              {tCommon("cancel")}
            </Button>
            <Button
              variant="destructive"
              data-testid={USER_DETAIL_TEST_IDS.reasonModalConfirm}
              onClick={submitDestructive}
              disabled={
                !destructiveModal.reason.trim() || destructiveModal.submitting
              }
            >
              {destructiveModal.submitting
                ? t("reasonModal.confirming")
                : t("reasonModal.confirmButton")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}
