"use client";

/**
 * One workspace's quota detail (Issue #325, #663, #712).
 *
 * Moved out of the Quotas page's expandable workspace row in #2765; the admin
 * user detail shows it under a workspace the user owns. Per resource: base
 * (the tier), add-on, and usage / effective. Edit add-ons and Edit spend cap
 * open their dialogs and refresh this view after a save.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { DollarSign, Settings } from "lucide-react";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import {
  getWorkspaceQuotas,
  type WorkspaceQuotaDetail as QuotaDetail,
} from "@/lib/api/admin";
import {
  ADDON_TYPES,
  READ_ONLY_QUOTAS,
  formatAddonValue,
  formatQuotaValue,
  formatReadOnlyQuotaValue,
} from "./_addon-types";
import { AddonEditDialog } from "./AddonEditDialog";
import { SpendCapEditDialog } from "./SpendCapEditDialog";

interface WorkspaceQuotaDetailProps {
  workspaceId: string;
  /** Bump to refetch (e.g. after the workspace's plan changed). */
  refreshKey?: number;
}

export function WorkspaceQuotaDetail({
  workspaceId,
  refreshKey = 0,
}: WorkspaceQuotaDetailProps) {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");
  const { toast } = useToast();
  const [quotaDetail, setQuotaDetail] = useState<QuotaDetail | null>(null);
  const [loading, setLoading] = useState(true);
  // A flag, not the message: the fetch must not depend on the translator.
  const [failed, setFailed] = useState(false);
  const [addonDialogOpen, setAddonDialogOpen] = useState(false);
  const [spendCapDialogOpen, setSpendCapDialogOpen] = useState(false);
  // A response lands only if no newer request superseded it.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setFailed(false);
    try {
      const detail = await getWorkspaceQuotas(workspaceId);
      if (seq === requestSeq.current) setQuotaDetail(detail);
    } catch {
      if (seq === requestSeq.current) setFailed(true);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  // After a save the change already succeeded; this refresh is best-effort,
  // so a failed refetch is a toast, not a replaced panel.
  const refreshAfterSave = async () => {
    try {
      setQuotaDetail(await getWorkspaceQuotas(workspaceId));
    } catch {
      toast({
        title: tCommon("error"),
        description: t("messages.quotaRefreshError"),
        variant: "destructive",
      });
    }
  };

  if (failed) {
    return <ErrorBanner error={t("quota.loadError")} />;
  }
  if (loading && !quotaDetail) {
    return <LoadingState lines={4} />;
  }
  if (!quotaDetail) return null;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-2 border-b pb-2 text-xs font-medium text-muted-foreground">
        <div>{t("quota.resource")}</div>
        <div className="text-right">
          {t("quota.base")} ({quotaDetail.plan_name})
        </div>
        <div className="text-right">{t("quota.addon")}</div>
        <div className="text-right">{t("quota.usageEffective")}</div>
      </div>
      {/* Issue #663: ADDON_TYPES drives the addon-bearing rows;
          READ_ONLY_QUOTAS appends tier-fixed dimensions with no addon. */}
      {ADDON_TYPES.map((meta) => {
        const base = quotaDetail.base[meta.baseField];
        const addon = quotaDetail.addon[meta.addonField];
        const effective = quotaDetail.effective[meta.baseField];
        const usage = meta.usageField
          ? quotaDetail.usage[meta.usageField]
          : null;
        return (
          <div key={meta.key} className="grid grid-cols-4 gap-2 text-sm">
            <div className="font-medium">{t(`quota.${meta.key}`)}</div>
            <div className="text-right text-muted-foreground">
              {formatQuotaValue(meta, base)}
            </div>
            <div className="text-right">
              {addon > 0 ? (
                <span className="text-green-600 dark:text-green-400">
                  {formatAddonValue(meta, addon)}
                </span>
              ) : (
                <span className="text-muted-foreground">-</span>
              )}
            </div>
            {/* Usage (used) is the numerator, matching "used / limit".
                Rows with no tracked usage show the effective value alone. */}
            <div className="text-right">
              {usage !== null ? (
                <>
                  <span className="font-medium">{usage.toLocaleString()}</span>
                  <span className="text-muted-foreground">
                    {" "}
                    / {formatQuotaValue(meta, effective)}
                  </span>
                </>
              ) : (
                <span className="font-medium">
                  {formatQuotaValue(meta, effective)}
                </span>
              )}
            </div>
          </div>
        );
      })}
      {READ_ONLY_QUOTAS.map((meta) => {
        const value = quotaDetail.base[meta.baseField];
        return (
          <div key={meta.key} className="grid grid-cols-4 gap-2 text-sm">
            <div className="font-medium">{t(`quota.${meta.key}`)}</div>
            <div className="text-right text-muted-foreground">
              {formatReadOnlyQuotaValue(meta, value)}
            </div>
            <div className="text-right text-muted-foreground">-</div>
            <div className="text-right">
              <span className="font-medium">
                {formatReadOnlyQuotaValue(meta, value)}
              </span>
            </div>
          </div>
        );
      })}
      <div className="flex gap-2 border-t pt-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setAddonDialogOpen(true)}
        >
          <Settings className="mr-1 h-4 w-4" />
          {t("actions.editAddons")}
        </Button>
        {quotaDetail.spend_cap && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setSpendCapDialogOpen(true)}
          >
            <DollarSign className="mr-1 h-4 w-4" />
            {t("actions.editSpendCap")}
          </Button>
        )}
      </div>

      {addonDialogOpen && (
        <AddonEditDialog
          quotaDetail={quotaDetail}
          onClose={() => setAddonDialogOpen(false)}
          onSaved={() => void refreshAfterSave()}
        />
      )}

      {/* Spend Cap Edit Dialog (Issue #712) */}
      {quotaDetail.spend_cap && (
        <SpendCapEditDialog
          open={spendCapDialogOpen}
          onOpenChange={setSpendCapDialogOpen}
          workspaceId={quotaDetail.workspace_id}
          workspaceName={quotaDetail.workspace_name}
          spendCap={quotaDetail.spend_cap}
          onSaved={() => void refreshAfterSave()}
        />
      )}
    </div>
  );
}
