"use client";

/**
 * Admin Plans Management Page
 *
 * Issue #149: Plan tier enforcement
 *
 * Allows admins to:
 * - View all workspaces with plan tiers and usage
 * - Change workspace plan tiers
 * - Set custom quota overrides
 * - View plan change audit log
 */

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useTabParam } from "@/hooks/useTabParam";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { PlanBadge } from "@/components/common/PlanBadge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  RefreshCw,
  Edit,
  CheckCircle,
  ChevronDown,
  Settings,
  Info,
  Inbox,
  DollarSign,
} from "lucide-react";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useToast } from "@/hooks/use-toast";
import {
  getAdminWorkspaces,
  getAdminPlanAudit,
  getAdminPlanTiers,
  updateWorkspacePlan,
  getWorkspaceQuotas,
  updateWorkspaceAddons,
  type WorkspacePlanInfo as WorkspacePlan,
  type PlanChangeAuditEntry as PlanChangeAudit,
  type PlanTierInfo,
  type WorkspaceQuotaDetail,
} from "@/lib/api/admin";
import {
  ADDON_TYPES,
  READ_ONLY_QUOTAS,
  EMPTY_ADDON_VALUES,
  buildUpdateAddonRequest,
  computeAddonEffectivePreview,
  findInvalidAddonValues,
  formatAddonValue,
  formatQuotaValue,
  formatReadOnlyQuotaValue,
  snapshotAddonValues,
  type AddonKey,
  type AddonValuesByKey,
} from "./_addon-types";
import { SpendCapEditDialog } from "./SpendCapEditDialog";

const PLAN_TABS = ["workspaces", "tiers", "audit"] as const;

// ============================================================================
// Tier comparison table — row definitions (Issue #664)
// ============================================================================

// Backend feature-flag identifiers (mirrors `PlanTier.features` strings in
// `backend/src/config/plan_tiers.py`). Kept as a `const` map so the row
// definitions reference symbolic names instead of bare strings — typos are
// caught at compile time and the mapping between row and feature is auditable.
const TIER_FEATURES = {
  RERANKING: "reranking",
  OAUTH: "oauth",
  PUBLIC_CONTEXTS: "public_contexts",
  MEMORY_ANALYSIS: "memory_analysis",
} as const;

type TierRowDef = {
  readonly key: string;
  readonly render: (tier: PlanTierInfo) => string;
};

const formatNumber = (value: number): string =>
  value === 0 ? "—" : value.toLocaleString();
const formatBool = (value: boolean): string => (value ? "✅" : "—");
const hasFeature = (tier: PlanTierInfo, name: string): string =>
  formatBool(tier.features.includes(name));
const formatStorage = (bytes: number): string => {
  if (bytes === 0) return "—";
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) {
    return Number.isInteger(gib) ? `${gib} GiB` : `${gib.toFixed(1)} GiB`;
  }
  return `${Math.round(bytes / 1024 ** 2)} MiB`;
};

// `as const satisfies` preserves literal `key` types for the derived
// `TierRowKey` union below while still constraining each entry's shape.
const TIER_ROW_DEFINITIONS = [
  {
    key: "contextsPerWorkspace",
    render: (t: PlanTierInfo) => formatNumber(t.max_contexts_per_workspace),
  },
  {
    key: "memories",
    render: (t: PlanTierInfo) => formatNumber(t.memory_limit),
  },
  {
    key: "mcpCallsPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.mcp_calls_per_day),
  },
  {
    key: "analysisRuns",
    render: (t: PlanTierInfo) => formatNumber(t.analysis_runs_per_day),
  },
  {
    key: "reranking",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.RERANKING),
  },
  {
    key: "mcpAppCredentials",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.OAUTH),
  },
  {
    key: "storage",
    render: (t: PlanTierInfo) => formatStorage(t.storage_limit_bytes),
  },
  {
    key: "maxMembers",
    render: (t: PlanTierInfo) => formatNumber(t.max_members_per_workspace),
  },
  {
    key: "maxResourceTokens",
    render: (t: PlanTierInfo) => formatNumber(t.max_resource_tokens),
  },
  {
    key: "restCallsPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.rest_calls_per_day),
  },
  {
    key: "publicCallsPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.public_calls_per_day),
  },
  {
    key: "boundPublicPerMinute",
    render: (t: PlanTierInfo) => formatNumber(t.bound_public_calls_per_minute),
  },
  {
    key: "sleepContextsLimit",
    render: (t: PlanTierInfo) => formatNumber(t.sleep_enabled_contexts_limit),
  },
  {
    key: "sharedContexts",
    render: (t: PlanTierInfo) => formatBool(t.allows_shared_contexts),
  },
  {
    key: "publicContexts",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.PUBLIC_CONTEXTS),
  },
  {
    key: "memoryAnalysis",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.MEMORY_ANALYSIS),
  },
] as const satisfies readonly TierRowDef[];

type TierRowKey = (typeof TIER_ROW_DEFINITIONS)[number]["key"];

export default function AdminPlansPage() {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");

  const [workspaces, setWorkspaces] = useState<WorkspacePlan[]>([]);
  const [auditLog, setAuditLog] = useState<PlanChangeAudit[]>([]);
  const [tiers, setTiers] = useState<PlanTierInfo[]>([]);
  const [tiersError, setTiersError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [changePlanDialogOpen, setChangePlanDialogOpen] = useState(false);
  const [selectedWorkspace, setSelectedWorkspace] =
    useState<WorkspacePlan | null>(null);
  const [newPlan, setNewPlan] = useState<string>("");
  const [expandedWorkspaceId, setExpandedWorkspaceId] = useState<string | null>(
    null,
  );
  const [quotaDetail, setQuotaDetail] = useState<WorkspaceQuotaDetail | null>(
    null,
  );
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [addonDialogOpen, setAddonDialogOpen] = useState(false);
  const [spendCapDialogOpen, setSpendCapDialogOpen] = useState(false);
  // Issue #663: 9 addon dimensions consolidated into a single record so
  // ``ADDON_TYPES`` can drive both the dialog inputs and the PUT body
  // construction without per-field useState plumbing.
  const [addonValues, setAddonValues] =
    useState<AddonValuesByKey>(EMPTY_ADDON_VALUES);
  const setAddonValue = (key: AddonKey, value: number): void => {
    setAddonValues((prev) => ({ ...prev, [key]: value }));
  };
  // #800: addon keys whose current form value can't be saved (non-multiple of
  // perUnit, or negative). Derived once from the live form state so the dialog
  // warning and the Save gate stay in lock-step and clear as the admin fixes
  // each value. Cheap pure scan of 9 entries — no memoization needed.
  const invalidAddons = findInvalidAddonValues(addonValues);
  const { toast } = useToast();
  const [tab, setTab] = useTabParam("workspaces", "tab", PLAN_TABS);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoading(true);
    setTiersError(null);
    try {
      // Issue #664: tier fetch is allSettled-isolated so a tier endpoint
      // failure surfaces inline on the tiers tab via ErrorBanner without
      // blocking the workspaces + audit fetches that drive the other tabs.
      const [workspacesResult, auditResult, tiersResult] =
        await Promise.allSettled([
          getAdminWorkspaces(),
          getAdminPlanAudit(100),
          getAdminPlanTiers(),
        ]);

      if (workspacesResult.status === "fulfilled") {
        setWorkspaces(workspacesResult.value);
      }
      if (auditResult.status === "fulfilled") {
        setAuditLog(auditResult.value);
      }
      if (tiersResult.status === "fulfilled") {
        setTiers(tiersResult.value);
      } else {
        setTiersError(t("tiersTable.loadError"));
      }

      // Workspaces + audit share the existing toast channel (page-level
      // failure scope). Tier failure is panel-scoped and uses ErrorBanner
      // — emitting both would double-fire the same incident.
      if (
        workspacesResult.status === "rejected" ||
        auditResult.status === "rejected"
      ) {
        toast({
          title: tCommon("error"),
          description: t("messages.loadError"),
          variant: "destructive",
        });
      }
    } finally {
      setLoading(false);
    }
  };

  const handleChangePlan = async () => {
    if (!selectedWorkspace || !newPlan) return;

    try {
      await updateWorkspacePlan(selectedWorkspace.id, {
        plan_name: newPlan as "free" | "basic" | "pro",
      });

      toast({
        title: tCommon("success"),
        description: t("messages.changeSuccess", {
          oldPlan: selectedWorkspace.plan_name.toUpperCase(),
          newPlan: newPlan.toUpperCase(),
        }),
      });

      setChangePlanDialogOpen(false);
      loadData();
    } catch (err) {
      console.error("Failed to change plan:", err);
      toast({
        title: tCommon("error"),
        description: t("messages.changeError"),
        variant: "destructive",
      });
    }
  };

  const toggleQuotaDetail = async (workspaceId: string) => {
    if (expandedWorkspaceId === workspaceId) {
      setExpandedWorkspaceId(null);
      setQuotaDetail(null);
      return;
    }
    setExpandedWorkspaceId(workspaceId);
    setQuotaLoading(true);
    try {
      const detail = await getWorkspaceQuotas(workspaceId);
      setQuotaDetail(detail);
    } catch {
      toast({
        title: tCommon("error"),
        description: "Failed to load quota details",
        variant: "destructive",
      });
    } finally {
      setQuotaLoading(false);
    }
  };

  const openAddonDialog = () => {
    if (!quotaDetail) return;
    setAddonValues(snapshotAddonValues(quotaDetail));
    setAddonDialogOpen(true);
  };

  const handleUpdateAddons = async () => {
    if (!quotaDetail) return;
    try {
      await updateWorkspaceAddons(
        quotaDetail.workspace_id,
        buildUpdateAddonRequest(addonValues),
      );
      toast({
        title: tCommon("success"),
        description: t("messages.addonUpdateSuccess"),
      });
      setAddonDialogOpen(false);
      // Refresh detail
      const detail = await getWorkspaceQuotas(quotaDetail.workspace_id);
      setQuotaDetail(detail);
      loadData();
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to update addons";
      toast({
        title: tCommon("error"),
        description: message,
        variant: "destructive",
      });
    }
  };

  const openChangePlanDialog = (workspace: WorkspacePlan) => {
    setSelectedWorkspace(workspace);
    setNewPlan(workspace.plan_name);
    setChangePlanDialogOpen(true);
  };

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <Button onClick={loadData} variant="outline" disabled={loading}>
            {loading ? (
              <InlineSpinner size="sm" className="mr-2" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            {tCommon("refresh")}
          </Button>
        }
      />

      <Tabs value={tab} onValueChange={setTab} className="mt-6">
        <TabsList>
          <TabsTrigger value={PLAN_TABS[0]}>{t("tabs.workspaces")}</TabsTrigger>
          <TabsTrigger value={PLAN_TABS[1]}>{t("tabs.tiers")}</TabsTrigger>
          <TabsTrigger value={PLAN_TABS[2]}>{t("tabs.audit")}</TabsTrigger>
        </TabsList>

        {/* Workspaces Tab */}
        <TabsContent value={PLAN_TABS[0]} className="mt-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("workspacesTable.title")}</CardTitle>
              <CardDescription>
                {t("workspacesTable.description")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="flex items-center justify-center py-12">
                  <InlineSpinner size="lg" />
                </div>
              ) : workspaces.length === 0 ? (
                <p className="text-center text-muted-foreground py-12">
                  {tCommon("noWorkspaces")}
                </p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("workspacesTable.workspace")}</TableHead>
                      <TableHead>{t("workspacesTable.plan")}</TableHead>
                      <TableHead className="text-right">
                        {t("workspacesTable.memories")}
                      </TableHead>
                      <TableHead>{t("workspacesTable.usage")}</TableHead>
                      <TableHead className="text-right">
                        {t("workspacesTable.actions")}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {workspaces.map((workspace) => {
                      const memoryPercentage =
                        (workspace.total_memories / workspace.memory_limit) *
                        100;
                      const maxPercentage = memoryPercentage;

                      const isExpanded = expandedWorkspaceId === workspace.id;

                      return (
                        <React.Fragment key={workspace.id}>
                          <TableRow
                            className="cursor-pointer hover:bg-muted/50"
                            onClick={() => toggleQuotaDetail(workspace.id)}
                          >
                            <TableCell>
                              <div className="flex items-center gap-2">
                                <ChevronDown
                                  className={`h-4 w-4 text-muted-foreground transition-transform ${isExpanded ? "rotate-180" : ""}`}
                                />
                                <div>
                                  <div className="font-medium">
                                    {workspace.name}
                                  </div>
                                  {workspace.owner_name && (
                                    <div className="text-xs text-muted-foreground mt-1">
                                      {t("workspacesTable.owner", {
                                        name: workspace.owner_name,
                                      })}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </TableCell>
                            <TableCell>
                              <PlanBadge
                                planName={
                                  workspace.plan_name as
                                    "free" | "basic" | "pro"
                                }
                              />
                            </TableCell>
                            <TableCell className="text-right">
                              {workspace.total_memories.toLocaleString()} /{" "}
                              {workspace.memory_limit.toLocaleString()}
                            </TableCell>
                            <TableCell>
                              <Badge
                                variant={
                                  maxPercentage >= 95
                                    ? "destructive"
                                    : maxPercentage >= 80
                                      ? "secondary"
                                      : "outline"
                                }
                              >
                                {maxPercentage.toFixed(1)}%
                              </Badge>
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  openChangePlanDialog(workspace);
                                }}
                              >
                                <Edit className="h-4 w-4 mr-1" />
                                {t("workspacesTable.changePlan")}
                              </Button>
                            </TableCell>
                          </TableRow>
                          {isExpanded && (
                            <TableRow>
                              <TableCell
                                colSpan={5}
                                className="bg-muted/30 p-0"
                              >
                                <div className="p-4">
                                  {quotaLoading ? (
                                    <div className="flex justify-center py-4">
                                      <InlineSpinner size="sm" />
                                    </div>
                                  ) : quotaDetail &&
                                    quotaDetail.workspace_id ===
                                      workspace.id ? (
                                    <div className="space-y-3">
                                      <div className="grid grid-cols-4 gap-2 text-xs font-medium text-muted-foreground border-b pb-2">
                                        <div>{t("quota.resource")}</div>
                                        <div className="text-right">
                                          {t("quota.base")} (
                                          {quotaDetail.plan_name})
                                        </div>
                                        <div className="text-right">
                                          {t("quota.addon")}
                                        </div>
                                        <div className="text-right">
                                          {t("quota.effectiveUsage")}
                                        </div>
                                      </div>
                                      {/* Issue #663: ADDON_TYPES drives the
                                          addon-bearing rows; READ_ONLY_QUOTAS
                                          appends tier-fixed dimensions (e.g.
                                          max_resource_tokens) with no addon
                                          column. Single source of truth shared
                                          with the addon edit dialog below. */}
                                      {ADDON_TYPES.map((meta) => {
                                        const base =
                                          quotaDetail.base[meta.baseField];
                                        const addon =
                                          quotaDetail.addon[meta.addonField];
                                        const effective =
                                          quotaDetail.effective[meta.baseField];
                                        const usage = meta.usageField
                                          ? quotaDetail.usage[meta.usageField]
                                          : null;
                                        return (
                                          <div
                                            key={meta.key}
                                            className="grid grid-cols-4 gap-2 text-sm"
                                          >
                                            <div className="font-medium">
                                              {t(`quota.${meta.key}`)}
                                            </div>
                                            <div className="text-right text-muted-foreground">
                                              {formatQuotaValue(meta, base)}
                                            </div>
                                            <div className="text-right">
                                              {addon > 0 ? (
                                                <span className="text-green-600 dark:text-green-400">
                                                  {formatAddonValue(
                                                    meta,
                                                    addon,
                                                  )}
                                                </span>
                                              ) : (
                                                <span className="text-muted-foreground">
                                                  -
                                                </span>
                                              )}
                                            </div>
                                            <div className="text-right">
                                              <span className="font-medium">
                                                {formatQuotaValue(
                                                  meta,
                                                  effective,
                                                )}
                                              </span>
                                              {usage !== null && (
                                                <span className="text-muted-foreground">
                                                  {" "}
                                                  / {usage.toLocaleString()}{" "}
                                                  {t("quota.used")}
                                                </span>
                                              )}
                                            </div>
                                          </div>
                                        );
                                      })}
                                      {READ_ONLY_QUOTAS.map((meta) => {
                                        const value =
                                          quotaDetail.base[meta.baseField];
                                        return (
                                          <div
                                            key={meta.key}
                                            className="grid grid-cols-4 gap-2 text-sm"
                                          >
                                            <div className="font-medium">
                                              {t(`quota.${meta.key}`)}
                                            </div>
                                            <div className="text-right text-muted-foreground">
                                              {formatReadOnlyQuotaValue(
                                                meta,
                                                value,
                                              )}
                                            </div>
                                            <div className="text-right text-muted-foreground">
                                              -
                                            </div>
                                            <div className="text-right">
                                              <span className="font-medium">
                                                {formatReadOnlyQuotaValue(
                                                  meta,
                                                  value,
                                                )}
                                              </span>
                                            </div>
                                          </div>
                                        );
                                      })}
                                      <div className="pt-2 border-t flex gap-2">
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            openAddonDialog();
                                          }}
                                        >
                                          <Settings className="h-4 w-4 mr-1" />
                                          {t("workspacesTable.editAddons")}
                                        </Button>
                                        {quotaDetail.spend_cap && (
                                          <Button
                                            size="sm"
                                            variant="outline"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              setSpendCapDialogOpen(true);
                                            }}
                                          >
                                            <DollarSign className="h-4 w-4 mr-1" />
                                            {t("workspacesTable.editSpendCap")}
                                          </Button>
                                        )}
                                      </div>
                                    </div>
                                  ) : null}
                                </div>
                              </TableCell>
                            </TableRow>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Plan Tiers Tab — Issue #664 */}
        <TabsContent value={PLAN_TABS[1]} className="mt-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("tiersTable.title")}</CardTitle>
              <CardDescription>{t("tiersTable.description")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <Alert>
                <Info className="h-4 w-4" />
                <AlertTitle>{t("tiersTable.infoCard.title")}</AlertTitle>
                <AlertDescription className="space-y-2">
                  <p>{t("tiersTable.infoCard.envOverrideBody")}</p>
                  <p>{t("tiersTable.infoCard.addonBody")}</p>
                  <p>{t("tiersTable.infoCard.zeroFloorBody")}</p>
                </AlertDescription>
              </Alert>

              <ErrorBanner error={tiersError} />

              {loading ? (
                <TableLoadingState rows={TIER_ROW_DEFINITIONS.length} />
              ) : tiersError ? null : tiers.length === 0 ? (
                // Empty state only renders for a successful-but-empty
                // response — fetch failures land in ErrorBanner above and
                // must not also fire an empty-state message (frontend.md
                // "one channel per error class").
                <EmptyState
                  icon={Inbox}
                  title={t("tiersTable.emptyTitle")}
                  description={t("tiersTable.emptyDescription")}
                  compact
                />
              ) : (
                <TooltipProvider delayDuration={200}>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t("tiersTable.feature")}</TableHead>
                        {tiers.map((tier) => (
                          <TableHead key={tier.name}>
                            {tier.display_name}
                          </TableHead>
                        ))}
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {TIER_ROW_DEFINITIONS.map((row) => (
                        <TableRow key={row.key}>
                          <TableCell className="font-medium">
                            <Tooltip>
                              <TooltipTrigger
                                className="text-left underline decoration-dotted underline-offset-4 cursor-help"
                                type="button"
                              >
                                {t(`tiersTable.${row.key}`)}
                              </TooltipTrigger>
                              <TooltipContent>
                                {t(`tiersTable.${row.key}Description`)}
                              </TooltipContent>
                            </Tooltip>
                          </TableCell>
                          {tiers.map((tier) => (
                            <TableCell key={tier.name}>
                              {row.render(tier)}
                            </TableCell>
                          ))}
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TooltipProvider>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Audit Log Tab */}
        <TabsContent value={PLAN_TABS[2]} className="mt-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("auditTable.title")}</CardTitle>
              <CardDescription>{t("auditTable.description")}</CardDescription>
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="flex items-center justify-center py-12">
                  <InlineSpinner size="lg" />
                </div>
              ) : auditLog.length === 0 ? (
                <p className="text-center text-muted-foreground py-12">
                  {tCommon("noChanges")}
                </p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("auditTable.date")}</TableHead>
                      <TableHead>{t("auditTable.workspace")}</TableHead>
                      <TableHead>{t("auditTable.change")}</TableHead>
                      <TableHead>{t("auditTable.changedBy")}</TableHead>
                      <TableHead>{t("auditTable.reason")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {auditLog.map((entry) => (
                      <TableRow key={entry.id}>
                        <TableCell>
                          {new Date(entry.changed_at).toLocaleString()}
                        </TableCell>
                        <TableCell>{entry.workspace_name}</TableCell>
                        <TableCell>
                          {entry.old_plan && (
                            <>
                              <PlanBadge
                                planName={
                                  entry.old_plan as "free" | "basic" | "pro"
                                }
                                size="sm"
                              />
                              <span className="mx-2">→</span>
                            </>
                          )}
                          <PlanBadge
                            planName={
                              entry.new_plan as "free" | "basic" | "pro"
                            }
                            size="sm"
                          />
                        </TableCell>
                        <TableCell>
                          {entry.changed_by_email ? (
                            <Link
                              href={`/admin/users/${entry.changed_by}`}
                              className="block hover:underline"
                            >
                              {entry.changed_by_name && (
                                <span className="block font-medium text-sm">
                                  {entry.changed_by_name}
                                </span>
                              )}
                              <span className="block text-xs text-gray-500">
                                {entry.changed_by_email}
                              </span>
                            </Link>
                          ) : (
                            entry.changed_by
                          )}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {entry.reason || "-"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Change Plan Dialog */}
      <Dialog
        open={changePlanDialogOpen}
        onOpenChange={setChangePlanDialogOpen}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("changePlanDialog.title")}</DialogTitle>
            <DialogDescription>
              {t("changePlanDialog.description")}{" "}
              <strong>{selectedWorkspace?.name}</strong>
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div>
              <label className="text-sm font-medium">
                {t("changePlanDialog.currentPlan")}
              </label>
              <div className="mt-2">
                {selectedWorkspace && (
                  <PlanBadge
                    planName={
                      selectedWorkspace.plan_name as "free" | "basic" | "pro"
                    }
                  />
                )}
              </div>
            </div>

            <div>
              <label className="text-sm font-medium">
                {t("changePlanDialog.newPlan")}
              </label>
              <Select value={newPlan} onValueChange={setNewPlan}>
                <SelectTrigger className="mt-2">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="free">{t("tiersTable.free")}</SelectItem>
                  <SelectItem value="basic">{t("tiersTable.basic")}</SelectItem>
                  <SelectItem value="pro">{t("tiersTable.pro")}</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {newPlan !== selectedWorkspace?.plan_name && (
              <div className="bg-yellow-50 dark:bg-yellow-950 p-3 rounded-md">
                <p className="text-sm text-yellow-800 dark:text-yellow-100">
                  {t("changePlanDialog.warning")}
                </p>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setChangePlanDialogOpen(false)}
            >
              {t("changePlanDialog.cancel")}
            </Button>
            <Button
              onClick={handleChangePlan}
              disabled={newPlan === selectedWorkspace?.plan_name}
            >
              <CheckCircle className="h-4 w-4 mr-2" />
              {t("changePlanDialog.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Addons Dialog (Issue #325) */}
      <Dialog open={addonDialogOpen} onOpenChange={setAddonDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("addonDialog.title")}</DialogTitle>
            <DialogDescription>
              {t("addonDialog.description")}{" "}
              <strong>{quotaDetail?.workspace_name}</strong>
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            {/* Issue #800: a legacy / broken cache can hold a value that is
                not a multiple of perUnit (e.g. pre-#665 memory_bonus 9000),
                which the backend rejects with HTTP 400 on save. Rather than
                surface an opaque 400, name the offending addons here and
                disable Save (below) until they are corrected. Driven by the
                live form state so it clears as the admin fixes each value.
                Informational gating notice, not an error channel
                (frontend.md) → Alert, not destructive. */}
            {invalidAddons.length > 0 && (
              <Alert>
                <AlertTitle>{t("addonDialog.invalidWarning.title")}</AlertTitle>
                <AlertDescription>
                  {t("addonDialog.invalidWarning.body", {
                    keys: invalidAddons
                      .map((key) => t(`addonDialog.${key}`))
                      .join(", "),
                  })}
                </AlertDescription>
              </Alert>
            )}

            {/* Issue #663: 9 addon inputs driven by ADDON_TYPES. The
                "effective" preview reflects the backend's _zero_floor
                clamp for PRO-only addons (sleep_contexts) on FREE/BASIC
                — admins see "0" before submitting, matching the LD-9
                "no effect on this tier" contract. */}
            {ADDON_TYPES.map((meta) => {
              const base = quotaDetail?.base[meta.baseField] ?? 0;
              const state = addonValues[meta.key];
              const usage = meta.usageField
                ? (quotaDetail?.usage[meta.usageField] ?? 0)
                : null;
              const effective = computeAddonEffectivePreview(meta, base, state);
              const perUnitHint = meta.unitSuffix
                ? t("addonDialog.perUnitWithSuffix", {
                    count: meta.perUnit,
                    unit: meta.unitSuffix,
                  })
                : t("addonDialog.perUnit", { count: meta.perUnit });
              return (
                <div key={meta.key}>
                  <Label htmlFor={`addon-${meta.key}`}>
                    {t(`addonDialog.${meta.key}`)}{" "}
                    <span className="text-xs text-muted-foreground font-normal">
                      {perUnitHint}
                    </span>
                    {meta.proOnly && (
                      <span className="ml-2 text-xs text-muted-foreground font-normal">
                        {t("addonDialog.proOnlyInline")}
                      </span>
                    )}
                  </Label>
                  <div className="flex items-center gap-2 mt-1">
                    <Input
                      id={`addon-${meta.key}`}
                      type="number"
                      min={0}
                      step={meta.perUnit}
                      value={state}
                      onChange={(e) =>
                        setAddonValue(meta.key, Number(e.target.value))
                      }
                    />
                    <span className="text-xs text-muted-foreground whitespace-nowrap">
                      {t("addonDialog.effective")}: {effective.toLocaleString()}
                      {meta.unitSuffix ? ` ${meta.unitSuffix}` : ""}
                      {usage !== null && (
                        <>
                          {" "}
                          ({usage} {t("addonDialog.used")})
                        </>
                      )}
                    </span>
                  </div>
                </div>
              );
            })}

            {/* Reduction warning scope (#663): only addons with a live
                usage counter can be reduced "below current usage" — the
                backend's LD-7 guard rejects them as HTTP 400. Pre-#663
                this covered members and contexts; #663 adds memory to
                the trigger since the same guard applies there. The
                other 6 addons have no per-workspace usage counter, so
                a client-side warning would have nothing to compare. */}
            {quotaDetail &&
              ADDON_TYPES.some(
                (meta) =>
                  meta.usageField !== undefined &&
                  addonValues[meta.key] < quotaDetail.addon[meta.addonField],
              ) && (
                <div className="bg-yellow-50 dark:bg-yellow-950 p-3 rounded-md">
                  <p className="text-sm text-yellow-800 dark:text-yellow-100">
                    {t("addonDialog.reductionWarning", {
                      memories: quotaDetail.usage.memories,
                      members: quotaDetail.usage.members,
                      contexts: quotaDetail.usage.contexts,
                    })}
                  </p>
                </div>
              )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setAddonDialogOpen(false)}>
              {tCommon("cancel")}
            </Button>
            {/* #800: block save while any addon value is non-saveable — the
                PUT sends all 9 fields, so an invalid one would 400 the whole
                request. Gating here keeps the warning and the save in sync. */}
            <Button
              onClick={handleUpdateAddons}
              disabled={invalidAddons.length > 0}
            >
              <CheckCircle className="h-4 w-4 mr-2" />
              {t("addonDialog.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Spend Cap Edit Dialog (Issue #712) */}
      {quotaDetail?.spend_cap && (
        <SpendCapEditDialog
          open={spendCapDialogOpen}
          onOpenChange={setSpendCapDialogOpen}
          workspaceId={quotaDetail.workspace_id}
          workspaceName={quotaDetail.workspace_name}
          spendCap={quotaDetail.spend_cap}
          onSaved={async () => {
            // The save already succeeded; this is a best-effort refresh of the
            // expanded panel. Catch so a failed refetch surfaces a toast
            // instead of an unhandled rejection + silently stale cap values.
            try {
              const detail = await getWorkspaceQuotas(quotaDetail.workspace_id);
              setQuotaDetail(detail);
            } catch {
              toast({
                title: tCommon("error"),
                description: t("messages.quotaRefreshError"),
                variant: "destructive",
              });
            }
          }}
        />
      )}
    </PageContainer>
  );
}
