"use client";

/**
 * App-wide capacity-over lock banner (#1941).
 *
 * A Free workspace that came back from a subscription holding more memories
 * or file bytes than Free allows is locked: search and saving are refused
 * (`CAPACITY-001`) until it is cleaned up or re-subscribed. The banner says
 * how much to remove and links to the plan page. It reads `capacity_lock`
 * from `GET /workspace/usage/current` and renders nothing when it is null,
 * when the read fails, or before it lands.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getWorkspaceUsageCurrent } from "@/lib/api/workspaces";
import type { CapacityLock } from "@/lib/api/usage";

export const PLAN_PAGE_PATH = "/workspace/settings/plan";

/** Megabytes for prose, rounded UP to 0.1 so "remove X MB" is enough. */
export function overageMegabytes(bytes: number): string {
  const tenths = Math.ceil((bytes * 10) / (1024 * 1024));
  return String(tenths / 10);
}

export function CapacityLockBannerView({ lock }: { lock: CapacityLock }) {
  const t = useTranslations("capacityLock");
  const memories =
    lock.over_memories > 0
      ? t("amountMemories", { count: lock.over_memories })
      : null;
  const storage =
    lock.over_bytes > 0
      ? t("amountStorage", { mb: overageMegabytes(lock.over_bytes) })
      : null;
  const amount =
    memories && storage
      ? t("amountBoth", { memories, storage })
      : (memories ?? storage ?? "");

  return (
    <Alert
      variant="destructive"
      className="rounded-none border-x-0 border-t-0"
      data-testid="capacity-lock-banner"
    >
      <AlertTriangle className="h-4 w-4" />
      <AlertTitle>{t("title")}</AlertTitle>
      <AlertDescription className="flex flex-wrap items-center gap-3">
        <span>{t("body", { amount })}</span>
        <Button asChild size="sm" variant="outline">
          <Link href={PLAN_PAGE_PATH}>{t("viewPlan")}</Link>
        </Button>
      </AlertDescription>
    </Alert>
  );
}

export function CapacityLockBanner() {
  const { currentWorkspaceId } = useWorkspace();
  const pathname = usePathname();
  const [lock, setLock] = useState<CapacityLock | null>(null);
  // While locked, re-read on navigation so a cleanup on one page clears the
  // banner on the next without a reload. While unlocked, only a workspace
  // switch re-reads: the usage read is not free, and it would run on every
  // page of every workspace that is never locked.
  const refreshKey = lock ? pathname : null;

  useEffect(() => {
    if (!currentWorkspaceId) {
      setLock(null);
      return;
    }
    let cancelled = false;
    getWorkspaceUsageCurrent()
      .then((usage) => {
        if (!cancelled) setLock(usage.capacity_lock ?? null);
      })
      .catch(() => {
        // The banner is advisory; a failed read must not break the page.
        if (!cancelled) setLock(null);
      });
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId, refreshKey]);

  if (!lock) return null;
  return <CapacityLockBannerView lock={lock} />;
}
