"use client";

/**
 * Account settings > Workspace management (#2919, account API of #2918).
 *
 * One place for an owner to see how many owned workspaces may be active, to
 * choose which ones to keep using, and to export, delete or resume a
 * suspended workspace. Reachable without a usable current workspace: the
 * layout's workspace guard exempts this route, so the page reads
 * `/system/info` directly instead of a workspace-scoped feature gate.
 *
 * One selection control at a time (design 12.7): while anything is suspended
 * it is the one for the current allowance (a save applies at once); with
 * nothing suspended it sits in the "if your paid subscriptions end" block.
 */

import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { ArrowLeft, Building2 } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
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
import { cn, spacing, typography } from "@/styles/design-tokens";
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

/** A page section whose heading can take focus. */
function PageSection({
  headingId,
  headingRef,
  title,
  description,
  children,
}: {
  headingId: string;
  headingRef?: RefObject<HTMLHeadingElement | null>;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={headingId} className={spacing.sectionMargin}>
      <h2
        id={headingId}
        ref={headingRef}
        tabIndex={-1}
        className={cn(typography.h3, "mb-1 focus:outline-none")}
      >
        {title}
      </h2>
      {description && (
        <p className={cn(typography.description, "mb-4")}>{description}</p>
      )}
      {children}
    </section>
  );
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
  const baseId = useId();
  const tableHeading = useRef<HTMLHeadingElement>(null);
  const selectionHeading = useRef<HTMLHeadingElement>(null);
  /** The control that opened the dialog, focused again when it closes. */
  const returnFocus = useRef<HTMLElement | null>(null);
  const [stale, setStale] = useState<"none" | "pending" | "refreshed">("none");
  const [dialogSeq, setDialogSeq] = useState(0);
  const [exporting, setExporting] = useState<SlotWorkspace | null>(null);
  const [deleting, setDeleting] = useState<SlotWorkspace | null>(null);
  const [resuming, setResuming] = useState<SlotWorkspace | null>(null);
  const [openingPlanFor, setOpeningPlanFor] = useState<string | null>(null);
  const [savedTick, setSavedTick] = useState(0);

  // After a save the selection control is rebuilt from the new answer: put
  // focus on its section heading (the table's when the section is gone).
  useEffect(() => {
    if (savedTick === 0) return;
    (selectionHeading.current ?? tableHeading.current)?.focus();
  }, [savedTick]);

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
  const page = (body: ReactNode) => (
    <PageContainer>
      {header}
      {body}
    </PageContainer>
  );

  if (info === null) return page(<TableLoadingState rows={3} />);
  if (isSystemInfoUnavailable(info)) {
    return page(
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
      </Alert>,
    );
  }
  if (!enabled || slots.unavailable) {
    return page(
      <Alert>
        <AlertDescription>{t("unavailable")}</AlertDescription>
      </Alert>,
    );
  }

  const data = slots.data;
  if (slots.loading && data === null) return page(<TableLoadingState rows={3} />);
  if (data === null) return page(<ErrorBanner error={t("loadFailed")} />);
  if (data.workspaces.length === 0) {
    return page(
      <EmptyState
        icon={Building2}
        title={t("empty.title")}
        description={t("empty.description")}
        actionLabel={t("empty.action")}
        onAction={() => router.push("/workspace/settings?create=true")}
      />,
    );
  }

  // The active set changed: the switcher and the guard read the workspace
  // list, so it is reloaded too, quietly (the page stays mounted).
  const applied = (next: WorkspaceSlots) => {
    setStale("none");
    slots.replace(next);
    void refreshWorkspaces({ quiet: true });
  };
  const onStale = () => {
    setStale("pending");
    void slots.reload({ background: true }).then((fresh) => {
      if (fresh) setStale("refreshed");
    });
  };

  const openDialog = (open: (w: SlotWorkspace) => void, w: SlotWorkspace) => {
    returnFocus.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setDialogSeq((n) => n + 1);
    open(w);
  };
  const restoreFocus = (event: Event) => {
    event.preventDefault();
    const opener = returnFocus.current;
    returnFocus.current = null;
    if (opener?.isConnected) opener.focus();
    else tableHeading.current?.focus();
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
  const onSaved = (next: WorkspaceSlots) => {
    applied(next);
    setSavedTick((n) => n + 1);
  };

  return page(
    <>
      {stale !== "none" && (
        <Alert variant="warning" className="mb-4">
          <AlertDescription>
            <p>{t("stale")}</p>
            {stale === "refreshed" && <p>{t("staleRefreshed")}</p>}
          </AlertDescription>
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

      <PageSection
        headingId={`${baseId}-table`}
        headingRef={tableHeading}
        title={t("table.caption")}
      >
        <OwnedWorkspacesTable
          workspaces={data.workspaces}
          labelledBy={`${baseId}-table`}
          planPage={planPage}
          openingPlanFor={openingPlanFor}
          onChangePlan={(w) => void changePlan(w)}
          onExport={(w) => openDialog(setExporting, w)}
          onDelete={(w) => openDialog(setDeleting, w)}
          onResume={(w) => openDialog(setResuming, w)}
        />
        <span role="status" aria-live="polite" className="sr-only">
          {openingPlanFor !== null ? t("planOpening") : ""}
        </span>
      </PageSection>

      {anySuspended && (
        <PageSection
          headingId={`${baseId}-current`}
          headingRef={selectionHeading}
          title={t("current.title")}
          description={t("current.description", { max: data.active_slots })}
        >
          <KeepSelection
            key={`current-${data.version}`}
            scenario="current"
            legend={t("keep.question")}
            workspaces={data.workspaces}
            initial={activeInSavedOrder(data)}
            max={data.active_slots}
            lockedIds={contracted.map((w) => w.id)}
            version={data.version}
            onSaved={onSaved}
            onStale={onStale}
          />
        </PageSection>
      )}

      {showCancellation && ifEnd && (
        <PageSection
          headingId={`${baseId}-end`}
          headingRef={anySuspended ? undefined : selectionHeading}
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
            {/* While anything is suspended a save applies at once, so the
                only selection control is the one for the current allowance. */}
            {!anySuspended && (
              <>
                <p className="text-sm text-muted-foreground">
                  {t("cancellation.optional")}
                </p>
                <KeepSelection
                  key={`end-${data.version}`}
                  scenario="paid_plans_end"
                  legend={t("keep.questionAfterEnd")}
                  workspaces={data.workspaces}
                  initial={ifEnd.active}
                  max={ifEnd.cap}
                  version={data.version}
                  onSaved={onSaved}
                  onStale={onStale}
                />
              </>
            )}
          </div>
        </PageSection>
      )}

      <ExportWorkspaceDialog
        key={`export-${dialogSeq}`}
        workspace={exporting}
        onClose={() => setExporting(null)}
        onCloseAutoFocus={restoreFocus}
      />
      <DeleteWorkspaceDialog
        key={`delete-${dialogSeq}`}
        workspace={deleting}
        onClose={() => setDeleting(null)}
        onCloseAutoFocus={restoreFocus}
        onDeleted={() => {
          // The row goes away: focus falls back to the table heading.
          returnFocus.current = null;
          setDeleting(null);
          void slots.reload({ background: true });
          void refreshWorkspaces({ quiet: true });
        }}
      />
      <ResumeWorkspaceDialog
        key={`resume-${dialogSeq}`}
        workspace={resuming}
        slots={data}
        onClose={() => setResuming(null)}
        onCloseAutoFocus={restoreFocus}
        onResumed={(next) => {
          // The row's actions go away: focus falls back to the table heading.
          returnFocus.current = null;
          setResuming(null);
          applied(next);
        }}
        onStale={onStale}
      />
    </>,
  );
}
