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
 *
 * Keyboard focus is never dropped to the page body: "Hide this notice" and a
 * re-check that hides the notice move it to the page's plan section
 * (`focusTargetRef`), and a re-check that finds the plan moves it to the
 * return link. "Check again" stays focusable while it runs.
 */

import {
  type RefObject,
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
import { useToast } from "@/hooks/use-toast";
import { useSystemInfo } from "@/hooks/useSystemFeatures";
import type { Workspace } from "@/lib/api/workspaces";
import {
  APPLICATION_RETURN_TOP_PLAN,
  PLAN_PAGE_PATH,
  forgetApplicationReturn,
  hasApplicationReturn,
  isApplicationReturnArrival,
  rememberApplicationReturn,
  subscribeApplicationReturn,
} from "@/lib/navigation/applicationReturn";
import { cn } from "@/lib/utils/cn";
import { httpsHref } from "@/lib/utils/httpsHref";

export interface ApplicationReturnNoticeProps {
  /** The workspace on screen (the one the plan button acts on). */
  workspace: Workspace;
  /** The deployment's label of the top plan. */
  planLabel: string;
  /** Called after "Check again" re-read the user and the workspaces. */
  onRechecked: () => void;
  /**
   * Where keyboard focus goes when the notice goes away (the page's plan
   * section, focusable with `tabIndex={-1}`).
   */
  focusTargetRef: RefObject<HTMLElement | null>;
  className?: string;
}

export function ApplicationReturnNotice({
  workspace,
  planLabel,
  onRechecked,
  focusTargetRef,
  className,
}: ApplicationReturnNoticeProps) {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
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
  // Refs read and written only in handlers and effects, never in render.
  const checkInFlight = useRef(false);
  const focusAfterCheck = useRef(false);
  const regionRef = useRef<HTMLDivElement>(null);
  const returnLinkRef = useRef<HTMLAnchorElement>(null);

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

  const visible = mayShow && user !== null && marked;
  const onPlan = workspace.plan_name === APPLICATION_RETURN_TOP_PLAN;

  // After a re-check that had the focus inside the notice: the return link
  // when the plan is found, the plan section when the notice went away.
  // Otherwise Check again is still there and keeps the focus.
  useEffect(() => {
    if (checking || !focusAfterCheck.current) return;
    focusAfterCheck.current = false;
    if (!visible) focusTargetRef.current?.focus();
    else if (onPlan) returnLinkRef.current?.focus();
  }, [checking, visible, onPlan, focusTargetRef]);

  if (href === null || user === null || !isCanonicalOwner || !marked)
    return null;

  const account = user.email || user.name;

  const handleCheckAgain = async () => {
    // aria-disabled, not disabled, while it runs: the button keeps the focus,
    // and a second activation is ignored here.
    if (checkInFlight.current) return;
    checkInFlight.current = true;
    const focusedInside = regionRef.current?.contains(document.activeElement) ?? false;
    setChecking(true);
    try {
      // Another tab may have switched the account; nothing syncs tabs.
      // refetchUser answers undefined on a transient failure (null is a
      // signed-out session, which the layout handles), and refreshWorkspaces
      // false when its read failed: then the notice says so instead of
      // showing the unchanged plan as if it had been checked.
      const userRead = (await refetchUser()) !== undefined;
      const listRead = userRead && (await refreshWorkspaces({ quiet: true })) !== false;
      if (!listRead) {
        toast({
          variant: "destructive",
          title: tCommon("error"),
          description: t("planPage.applicationReturn.checkFailed"),
        });
        return;
      }
      // Move the focus afterwards only if it was in the notice and still is,
      // or was dropped because what held it went away meanwhile.
      const active = document.activeElement;
      focusAfterCheck.current =
        focusedInside &&
        (active === null ||
          active === document.body ||
          (regionRef.current?.contains(active) ?? false));
      onRechecked();
      setRechecked(true);
    } finally {
      checkInFlight.current = false;
      setChecking(false);
    }
  };

  const handleHide = () => {
    focusTargetRef.current?.focus();
    forgetApplicationReturn();
  };

  return (
    <Alert
      ref={regionRef}
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
              <a
                ref={returnLinkRef}
                href={href}
                rel="noopener noreferrer"
                referrerPolicy="no-referrer"
              >
                {t("planPage.applicationReturn.returnLink")}
              </a>
            </Button>
          ) : (
            <>
              <Button
                variant="outline"
                onClick={() => void handleCheckAgain()}
                aria-disabled={checking}
                aria-busy={checking}
                className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
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
          <Button variant="ghost" onClick={handleHide}>
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
