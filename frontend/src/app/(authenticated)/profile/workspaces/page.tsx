"use client";

/**
 * Account settings > Workspace management (#2919, account API of #2918).
 *
 * One place for an owner to see how many owned workspaces may be active, to
 * choose which ones to keep using (now, or for when a paid subscription
 * ends), and to export, delete or resume a suspended workspace. Reachable
 * without a usable current workspace: the layout's workspace guard exempts
 * this route, so the page reads `/system/info` directly instead of a
 * workspace-scoped feature gate.
 */

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { ArrowLeft, Building2 } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { Section } from "@/components/common/Section";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import {
  isSystemInfoUnavailable,
  useSystemInfo,
} from "@/hooks/useSystemFeatures";
import { ApiError } from "@/lib/api/base";
import { mintBillingHandoff } from "@/lib/api/billing";
import type { SlotWorkspace, WorkspaceSlots } from "@/lib/api/workspaceSlots";
import { useWorkspaceSlots } from "@/components/workspaces/slots/useWorkspaceSlots";
import { OwnedWorkspacesTable } from "@/components/workspaces/slots/OwnedWorkspacesTable";
import { KeepSelection } from "@/components/workspaces/slots/KeepSelection";
import { ExportWorkspaceDialog } from "@/components/workspaces/slots/ExportWorkspaceDialog";
import { DeleteWorkspaceDialog } from "@/components/workspaces/slots/DeleteWorkspaceDialog";
import { ResumeWorkspaceDialog } from "@/components/workspaces/slots/ResumeWorkspaceDialog";

const PLAN_PAGE = "/workspace/settings/plan";

/** Active ids, the saved selection's order first. */
function activeInSavedOrder(slots: WorkspaceSlots): string[] {
  const active = slots.workspaces
    .filter((w) => w.status === "active")
    .map((w) => w.id);
  const saved = slots.selection.workspace_ids.filter((id) => active.includes(id));
  return [...saved, ...active.filter((id) => !saved.includes(id))];
}

export default function WorkspaceManagementPage() {
  const t = useTranslations("workspaceSlots");
  const access = useTranslations("featureAccess");
  const router = useRouter();
  const { toast } = useToast();
  const info = useSystemInfo();
  const enabled = info?.features?.workspace_slots === true;
  const planPage = info?.features?.plan_page === true;
  const { currentWorkspaceId, switchWorkspace, refreshWorkspaces } =
    useWorkspace();
  const slots = useWorkspaceSlots(enabled);
  const [stale, setStale] = useState(false);
  const [exporting, setExporting] = useState<SlotWorkspace | null>(null);
  const [deleting, setDeleting] = useState<SlotWorkspace | null>(null);
  const [resuming, setResuming] = useState<SlotWorkspace | null>(null);
  const [openingPlanFor, setOpeningPlanFor] = useState<string | null>(null);

  const header = (
    <>
      <Link
        href="/profile"
        className="mb-4 inline-flex items-center gap-1 text-sm text-slate-600 hover:underline dark:text-slate-300"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        {t("backToAccount")}
      </Link>
      <PageHeader title={t("title")} description={t("description")} />
    </>
  );

  if (info === null) {
    return (
      <PageContainer>
        {header}
        <TableLoadingState rows={3} />
      </PageContainer>
    );
  }
  if (isSystemInfoUnavailable(info)) {
    return (
      <PageContainer>
        {header}
        <Alert>
          <AlertDescription>
            {access("lookupFailed")}{" "}
            <Button
              variant="outline"
              size="sm"
              onClick={() => window.location.reload()}
            >
              {access("retry")}
            </Button>
          </AlertDescription>
        </Alert>
      </PageContainer>
    );
  }
  if (!enabled) {
    return (
      <PageContainer>
        {header}
        <Alert>
          <AlertDescription>{t("unavailable")}</AlertDescription>
        </Alert>
      </PageContainer>
    );
  }

  const data = slots.data;
  if (slots.loading && data === null) {
    return (
      <PageContainer>
        {header}
        <TableLoadingState rows={3} />
      </PageContainer>
    );
  }
  if (data === null) {
    return (
      <PageContainer>
        {header}
        <ErrorBanner error={t("loadFailed")} />
      </PageContainer>
    );
  }
  if (data.workspaces.length === 0) {
    return (
      <PageContainer>
        {header}
        <EmptyState
          icon={Building2}
          title={t("empty.title")}
          description={t("empty.description")}
          actionLabel={t("empty.action")}
          onAction={() => router.push("/workspace/settings?create=true")}
        />
      </PageContainer>
    );
  }

  // The active set changed: the switcher and the guard read the workspace
  // list, so it is reloaded too.
  const applied = (next: WorkspaceSlots) => {
    setStale(false);
    slots.replace(next);
    void refreshWorkspaces();
  };
  const onStale = () => {
    setStale(true);
    void slots.reload({ background: true });
  };

  const openBilling = async (workspaceId: string) => {
    const res = await mintBillingHandoff(workspaceId);
    if (res.url) {
      window.location.href = res.url;
      return;
    }
    toast({ variant: "destructive", title: t("planUnavailable") });
  };

  // One plan action at a time; a failure is a toast (a user action).
  const planAction = async (workspaceId: string, run: () => Promise<void>) => {
    if (openingPlanFor !== null) return;
    setOpeningPlanFor(workspaceId);
    try {
      await run();
    } catch (e) {
      toast({
        variant: "destructive",
        title:
          !(e instanceof ApiError && e.status === 503) &&
          e instanceof Error &&
          e.message
            ? e.message
            : t("planUnavailable"),
      });
    } finally {
      setOpeningPlanFor(null);
    }
  };

  // The plan page is scoped to the current workspace: an active workspace is
  // switched to first; a suspended one cannot be, so it goes through billing.
  const changePlan = (w: SlotWorkspace) =>
    planAction(w.id, async () => {
      if (w.status === "slot_suspended") {
        await openBilling(w.id);
        return;
      }
      if (currentWorkspaceId !== w.id) await switchWorkspace(w.id);
      router.push(PLAN_PAGE);
    });

  const anySuspended = data.workspaces.some((w) => w.status === "slot_suspended");
  const ifEnd = data.if_paid_plans_end;
  const showCancellation = ifEnd !== null && ifEnd.suspended.length > 0;
  const names = new Map(data.workspaces.map((w) => [w.id, w.name]));
  const contracted = data.workspaces.filter((w) => w.under_billing_contract);

  return (
    <PageContainer>
      {header}

      {stale && (
        <Alert variant="warning" className="mb-4">
          <AlertDescription>{t("stale")}</AlertDescription>
        </Alert>
      )}

      {data.selection_required && (
        <Alert variant="warning" className="mb-4">
          <AlertTitle>{t("selectionRequired.title")}</AlertTitle>
          <AlertDescription>{t("selectionRequired.description")}</AlertDescription>
        </Alert>
      )}

      <section
        aria-label={t("summary.label")}
        className="mb-6 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800"
      >
        <p className="text-2xl font-semibold">
          {t("summary.active", {
            active: data.active_count,
            slots: data.active_slots,
          })}
        </p>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {t("summary.owned", { count: data.owned_count })}
        </p>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          {t("summary.sharedNotCounted")}
        </p>
      </section>

      <Section title={t("table.caption")}>
        <OwnedWorkspacesTable
          workspaces={data.workspaces}
          planPage={planPage}
          openingPlanFor={openingPlanFor}
          onChangePlan={(w) => void changePlan(w)}
          onExport={setExporting}
          onDelete={setDeleting}
          onResume={setResuming}
        />
        <span role="status" aria-live="polite" className="sr-only">
          {openingPlanFor !== null ? t("planOpening") : ""}
        </span>
      </Section>

      {anySuspended && (
        <Section
          title={t("current.title")}
          description={t("current.description", { max: data.active_slots })}
        >
          <KeepSelection
            key={`current-${data.version}`}
            scenario="current"
            workspaces={data.workspaces}
            initial={activeInSavedOrder(data)}
            max={data.active_slots}
            lockedIds={contracted.map((w) => w.id)}
            version={data.version}
            onSaved={applied}
            onStale={onStale}
          />
        </Section>
      )}

      {showCancellation && ifEnd && (
        <Section
          title={t("cancellation.title")}
          description={t("cancellation.description", { cap: ifEnd.cap })}
        >
          <div className="space-y-4">
            {planPage && contracted.length > 0 && (
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  void planAction(contracted[0].id, () =>
                    openBilling(contracted[0].id),
                  )
                }
                disabled={openingPlanFor !== null}
              >
                {t("cancellation.openBilling")}
              </Button>
            )}
            <div>
              <p className="text-sm">{t("cancellation.wouldSuspend")}</p>
              <ul className="list-inside list-disc text-sm">
                {ifEnd.suspended.map((id) => (
                  <li key={id}>{names.get(id) ?? id}</li>
                ))}
              </ul>
            </div>
            <p className="text-sm text-muted-foreground">
              {t("cancellation.optional")}
            </p>
            <KeepSelection
              key={`end-${data.version}`}
              scenario="paid_plans_end"
              workspaces={data.workspaces}
              initial={ifEnd.active}
              max={ifEnd.cap}
              version={data.version}
              onSaved={applied}
              onStale={onStale}
            />
          </div>
        </Section>
      )}

      <ExportWorkspaceDialog
        key={`export-${exporting?.id ?? ""}`}
        workspace={exporting}
        onClose={() => setExporting(null)}
      />
      <DeleteWorkspaceDialog
        workspace={deleting}
        onClose={() => setDeleting(null)}
        onDeleted={() => {
          setDeleting(null);
          void slots.reload({ background: true });
          void refreshWorkspaces();
        }}
      />
      <ResumeWorkspaceDialog
        key={`resume-${resuming?.id ?? ""}`}
        workspace={resuming}
        slots={data}
        onClose={() => setResuming(null)}
        onResumed={(next) => {
          setResuming(null);
          applied(next);
        }}
        onStale={onStale}
      />
    </PageContainer>
  );
}
