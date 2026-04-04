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
} from "lucide-react";
import { InlineSpinner } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import {
  getAdminWorkspaces,
  getAdminPlanAudit,
  updateWorkspacePlan,
  getWorkspaceQuotas,
  updateWorkspaceAddons,
  type WorkspacePlanInfo as WorkspacePlan,
  type PlanChangeAuditEntry as PlanChangeAudit,
  type WorkspaceQuotaDetail,
} from "@/lib/api/admin";

export default function AdminPlansPage() {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");

  const [workspaces, setWorkspaces] = useState<WorkspacePlan[]>([]);
  const [auditLog, setAuditLog] = useState<PlanChangeAudit[]>([]);
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
  const [addonMemory, setAddonMemory] = useState(0);
  const [addonMcp, setAddonMcp] = useState(0);
  const [addonMember, setAddonMember] = useState(0);
  const [addonContext, setAddonContext] = useState(0);
  const { toast } = useToast();

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoading(true);
    try {
      const [workspaces, audit] = await Promise.all([
        getAdminWorkspaces(),
        getAdminPlanAudit(100),
      ]);

      setWorkspaces(workspaces);
      setAuditLog(audit);
    } catch (err) {
      console.error("Failed to load admin data:", err);
      toast({
        title: tCommon("error"),
        description: t("messages.loadError"),
        variant: "destructive",
      });
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
    setAddonMemory(quotaDetail.addon.memory_bonus);
    setAddonMcp(quotaDetail.addon.mcp_quota_bonus);
    setAddonMember(quotaDetail.addon.member_bonus);
    setAddonContext(quotaDetail.addon.context_bonus);
    setAddonDialogOpen(true);
  };

  const handleUpdateAddons = async () => {
    if (!quotaDetail) return;
    try {
      await updateWorkspaceAddons(quotaDetail.workspace_id, {
        addon_memory_bonus: addonMemory,
        addon_mcp_quota_bonus: addonMcp,
        addon_member_bonus: addonMember,
        addon_context_bonus: addonContext,
      });
      toast({ title: tCommon("success"), description: "Quota addons updated" });
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

      <Tabs defaultValue="workspaces" className="mt-6">
        <TabsList>
          <TabsTrigger value="workspaces">{t("tabs.workspaces")}</TabsTrigger>
          <TabsTrigger value="tiers">{t("tabs.tiers")}</TabsTrigger>
          <TabsTrigger value="audit">{t("tabs.audit")}</TabsTrigger>
        </TabsList>

        {/* Workspaces Tab */}
        <TabsContent value="workspaces" className="mt-6">
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
                                      Owner: {workspace.owner_name}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </TableCell>
                            <TableCell>
                              <PlanBadge
                                planName={
                                  workspace.plan_name as
                                    | "free"
                                    | "basic"
                                    | "pro"
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
                                        <div>Resource</div>
                                        <div className="text-right">
                                          Base ({quotaDetail.plan_name})
                                        </div>
                                        <div className="text-right">Addon</div>
                                        <div className="text-right">
                                          Effective / Usage
                                        </div>
                                      </div>
                                      {[
                                        {
                                          label: "Memories",
                                          base: quotaDetail.base.memory_limit,
                                          addon: quotaDetail.addon.memory_bonus,
                                          effective:
                                            quotaDetail.effective.memory_limit,
                                          usage: quotaDetail.usage.memories,
                                        },
                                        {
                                          label: "MCP Calls/day",
                                          base: quotaDetail.base
                                            .mcp_calls_per_day,
                                          addon:
                                            quotaDetail.addon.mcp_quota_bonus,
                                          effective:
                                            quotaDetail.effective
                                              .mcp_calls_per_day,
                                          usage: null,
                                        },
                                        {
                                          label: "Contexts",
                                          base: quotaDetail.base.max_contexts,
                                          addon:
                                            quotaDetail.addon.context_bonus,
                                          effective:
                                            quotaDetail.effective.max_contexts,
                                          usage: quotaDetail.usage.contexts,
                                        },
                                        {
                                          label: "Members",
                                          base: quotaDetail.base.max_members,
                                          addon: quotaDetail.addon.member_bonus,
                                          effective:
                                            quotaDetail.effective.max_members,
                                          usage: quotaDetail.usage.members,
                                        },
                                      ].map((row) => (
                                        <div
                                          key={row.label}
                                          className="grid grid-cols-4 gap-2 text-sm"
                                        >
                                          <div className="font-medium">
                                            {row.label}
                                          </div>
                                          <div className="text-right text-muted-foreground">
                                            {row.base.toLocaleString()}
                                          </div>
                                          <div className="text-right">
                                            {row.addon > 0 ? (
                                              <span className="text-green-600 dark:text-green-400">
                                                +{row.addon.toLocaleString()}
                                              </span>
                                            ) : (
                                              <span className="text-muted-foreground">
                                                -
                                              </span>
                                            )}
                                          </div>
                                          <div className="text-right">
                                            <span className="font-medium">
                                              {row.effective.toLocaleString()}
                                            </span>
                                            {row.usage !== null && (
                                              <span className="text-muted-foreground">
                                                {" "}
                                                / {row.usage.toLocaleString()}{" "}
                                                used
                                              </span>
                                            )}
                                          </div>
                                        </div>
                                      ))}
                                      <div className="pt-2 border-t">
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            openAddonDialog();
                                          }}
                                        >
                                          <Settings className="h-4 w-4 mr-1" />
                                          Edit Addons
                                        </Button>
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

        {/* Plan Tiers Tab */}
        <TabsContent value="tiers" className="mt-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("tiersTable.title")}</CardTitle>
              <CardDescription>{t("tiersTable.description")}</CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("tiersTable.feature")}</TableHead>
                    <TableHead>{t("tiersTable.free")}</TableHead>
                    <TableHead>{t("tiersTable.basic")}</TableHead>
                    <TableHead>{t("tiersTable.pro")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell className="font-medium">
                      {t("tiersTable.contextsPerWorkspace")}
                    </TableCell>
                    <TableCell>1</TableCell>
                    <TableCell>3</TableCell>
                    <TableCell>30</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium">
                      {t("tiersTable.memories")}
                    </TableCell>
                    <TableCell>1,000</TableCell>
                    <TableCell>10,000</TableCell>
                    <TableCell>100,000</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium">
                      {t("tiersTable.apiCalls")}
                    </TableCell>
                    <TableCell>100</TableCell>
                    <TableCell>2,000</TableCell>
                    <TableCell>10,000</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium">
                      {t("tiersTable.reranking")}
                    </TableCell>
                    <TableCell>-</TableCell>
                    <TableCell>✅</TableCell>
                    <TableCell>✅</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium">
                      {t("tiersTable.mcpAppCredentials")}
                    </TableCell>
                    <TableCell>✅</TableCell>
                    <TableCell>✅</TableCell>
                    <TableCell>✅</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium">
                      {t("tiersTable.memoryAgent")}
                    </TableCell>
                    <TableCell>-</TableCell>
                    <TableCell>-</TableCell>
                    <TableCell>✅ {t("tiersTable.unlimited")}</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Audit Log Tab */}
        <TabsContent value="audit" className="mt-6">
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
                        <TableCell>{entry.changed_by}</TableCell>
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
            <DialogTitle>Edit Quota Addons</DialogTitle>
            <DialogDescription>
              Adjust addon bonuses for{" "}
              <strong>{quotaDetail?.workspace_name}</strong>
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div>
              <Label htmlFor="addon-memory">Memory Bonus</Label>
              <div className="flex items-center gap-2 mt-1">
                <Input
                  id="addon-memory"
                  type="number"
                  min={0}
                  value={addonMemory}
                  onChange={(e) => setAddonMemory(Number(e.target.value))}
                />
                <span className="text-xs text-muted-foreground whitespace-nowrap">
                  Effective:{" "}
                  {(
                    (quotaDetail?.base.memory_limit ?? 0) + addonMemory
                  ).toLocaleString()}
                </span>
              </div>
            </div>

            <div>
              <Label htmlFor="addon-mcp">MCP Calls/day Bonus</Label>
              <div className="flex items-center gap-2 mt-1">
                <Input
                  id="addon-mcp"
                  type="number"
                  min={0}
                  value={addonMcp}
                  onChange={(e) => setAddonMcp(Number(e.target.value))}
                />
                <span className="text-xs text-muted-foreground whitespace-nowrap">
                  Effective:{" "}
                  {(
                    (quotaDetail?.base.mcp_calls_per_day ?? 0) + addonMcp
                  ).toLocaleString()}
                </span>
              </div>
            </div>

            <div>
              <Label htmlFor="addon-member">Member Bonus</Label>
              <div className="flex items-center gap-2 mt-1">
                <Input
                  id="addon-member"
                  type="number"
                  min={0}
                  value={addonMember}
                  onChange={(e) => setAddonMember(Number(e.target.value))}
                />
                <span className="text-xs text-muted-foreground whitespace-nowrap">
                  Effective:{" "}
                  {(
                    (quotaDetail?.base.max_members ?? 0) + addonMember
                  ).toLocaleString()}{" "}
                  ({quotaDetail?.usage.members ?? 0} used)
                </span>
              </div>
            </div>

            <div>
              <Label htmlFor="addon-context">Extra Contexts</Label>
              <div className="flex items-center gap-2 mt-1">
                <Input
                  id="addon-context"
                  type="number"
                  min={0}
                  value={addonContext}
                  onChange={(e) => setAddonContext(Number(e.target.value))}
                />
                <span className="text-xs text-muted-foreground whitespace-nowrap">
                  Effective:{" "}
                  {(
                    (quotaDetail?.base.max_contexts ?? 0) + addonContext
                  ).toLocaleString()}{" "}
                  ({quotaDetail?.usage.contexts ?? 0} used)
                </span>
              </div>
            </div>

            {quotaDetail &&
              (addonMember < quotaDetail.addon.member_bonus ||
                addonContext < quotaDetail.addon.context_bonus) && (
                <div className="bg-yellow-50 dark:bg-yellow-950 p-3 rounded-md">
                  <p className="text-sm text-yellow-800 dark:text-yellow-100">
                    Reducing member or context addons will be rejected if
                    current usage exceeds the new effective limit. (Members:{" "}
                    {quotaDetail.usage.members} used, Contexts:{" "}
                    {quotaDetail.usage.contexts} used)
                  </p>
                </div>
              )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setAddonDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleUpdateAddons}>
              <CheckCircle className="h-4 w-4 mr-2" />
              Save Addons
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}
