"use client";

/**
 * The link back to the application on the plan page (#2950).
 *
 * Shown while this tab holds a valid marker (see lib/navigation/
 * applicationReturn.ts), the deployment sets an https return address
 * (`KAGURA_PUBLIC_RETURN_URL`, through system info) and the signed-in user is
 * the workspace's canonical owner. It states the signed-in account, the
 * workspace on screen and whether that workspace is on the top plan, read
 * from KMC's own data, and offers a plain same-tab link to the configured
 * address. Nothing here navigates by itself, and the address is never taken
 * from the URL, storage or a request.
 *
 * An arrival (`?return=kagura-public`) is read once, when the notice mounts:
 * one effect writes the marker and takes the parameter out of the address bar,
 * and only when the notice may show. With no address set it renders nothing,
 * writes nothing and leaves the URL alone.
 */

import {
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useSystemInfo } from "@/hooks/useSystemFeatures";
import type { Workspace } from "@/lib/api/workspaces";
import {
  PLAN_PAGE_PATH,
  forgetApplicationReturn,
  hasApplicationReturn,
  isApplicationReturnArrival,
  rememberApplicationReturn,
  subscribeApplicationReturn,
} from "@/lib/navigation/applicationReturn";
import { cn } from "@/lib/utils/cn";
import { httpsHref } from "@/lib/utils/httpsHref";

/** The top tier, by the workspace picker's rule for the application. */
const TOP_PLAN = "promax";

export interface ApplicationReturnNoticeProps {
  /** The workspace on screen (the one the plan button acts on). */
  workspace: Workspace;
  /** The deployment's label of the top plan. */
  planLabel: string;
  /** Called after "Check again" re-read the user and the workspaces. */
  onRechecked: () => void;
  className?: string;
}

export function ApplicationReturnNotice({
  workspace,
  planLabel,
  onRechecked,
  className,
}: ApplicationReturnNoticeProps) {
  const t = useTranslations("workspace");
  const { user, refetchUser } = useAuth();
  const { refreshWorkspaces } = useWorkspace();
  const info = useSystemInfo();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const titleId = useId();

  // Read once, when the notice mounts; later changes of the URL are not read.
  const [arrival] = useState(() =>
    isApplicationReturnArrival(pathname, searchParams),
  );
  const marked = useSyncExternalStore(
    subscribeApplicationReturn,
    hasApplicationReturn,
    () => false,
  );
  const written = useRef(false);
  const [checking, setChecking] = useState(false);
  const [rechecked, setRechecked] = useState(false);

  const href = httpsHref(info?.kagura_public_return_url);
  const isCanonicalOwner =
    user !== null &&
    workspace.owner_user_id === user.id &&
    workspace.current_user_role === "owner";
  const mayShow = href !== null && isCanonicalOwner;

  useEffect(() => {
    if (!arrival || !mayShow || written.current) return;
    written.current = true;
    rememberApplicationReturn();
    router.replace(PLAN_PAGE_PATH, { scroll: false });
  }, [arrival, mayShow, router]);

  if (href === null || user === null || !isCanonicalOwner || !marked)
    return null;

  const onPlan = workspace.plan_name === TOP_PLAN;
  const account = user.email || user.name;

  const handleCheckAgain = async () => {
    setChecking(true);
    try {
      // Another tab may have switched the account; nothing syncs tabs.
      await refetchUser();
      await refreshWorkspaces({ quiet: true });
      onRechecked();
      setRechecked(true);
    } finally {
      setChecking(false);
    }
  };

  return (
    <Alert
      role="region"
      aria-labelledby={titleId}
      className={cn("mb-6", className)}
    >
      <AlertTitle id={titleId}>
        {t("planPage.applicationReturn.title")}
      </AlertTitle>
      <AlertDescription className="space-y-2">
        {account && (
          <p>{t("planPage.applicationReturn.signedInAs", { account })}</p>
        )}
        <p>
          {onPlan
            ? t("planPage.applicationReturn.onPlan", {
                workspace: workspace.name,
                plan: planLabel,
              })
            : t("planPage.applicationReturn.notOnPlan", {
                workspace: workspace.name,
                plan: planLabel,
              })}
        </p>
        <p>{t("planPage.applicationReturn.switchHint")}</p>
        <div className="flex flex-wrap items-center gap-3 pt-1">
          {onPlan ? (
            <Button asChild>
              <a href={href} rel="noopener noreferrer" referrerPolicy="no-referrer">
                {t("planPage.applicationReturn.returnLink")}
              </a>
            </Button>
          ) : (
            <>
              <Button
                variant="outline"
                onClick={() => void handleCheckAgain()}
                disabled={checking}
                aria-busy={checking}
              >
                {t("planPage.applicationReturn.checkAgain")}
              </Button>
              <a
                href={href}
                rel="noopener noreferrer"
                referrerPolicy="no-referrer"
                className="inline-flex min-h-9 items-center text-sm font-medium text-primary underline underline-offset-4 hover:text-primary/80"
              >
                {t("planPage.applicationReturn.returnLink")}
              </a>
            </>
          )}
          <Button variant="ghost" onClick={forgetApplicationReturn}>
            {t("planPage.applicationReturn.dismiss")}
          </Button>
        </div>
        <span role="status" aria-live="polite" className="sr-only">
          {rechecked && onPlan
            ? t("planPage.applicationReturn.nowOnPlan", { plan: planLabel })
            : ""}
        </span>
      </AlertDescription>
    </Alert>
  );
}
