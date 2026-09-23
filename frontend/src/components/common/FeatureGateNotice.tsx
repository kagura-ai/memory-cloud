"use client";

/**
 * FeatureGateNotice (#1646) — the one way to tell a member why they cannot
 * use something.
 *
 * It renders a `FeatureGate` descriptor (`lib/gates/featureGates.ts`, from
 * `useFeatureGate`, `useErrorGate`, `quotaGate` or `gateFromFacts`) with the
 * `gate.*` messages, so every notice reads the same whatever produced the
 * refusal:
 *
 * - `inline`  — an Alert (`upsell` for plan, `warning` for quota, default
 *               otherwise) above the thing that cannot be used.
 * - `page`    — an EmptyState for a whole surface; with `pageTitle` it wraps
 *               itself in PageContainer + PageHeader, without it it drops
 *               into an existing page or tab.
 * - `control` — a badge and a one-line hint beside a control that is
 *               disabled; pass `id` and point the control's
 *               `aria-describedby` at it.
 *
 * Hard rules, pinned by FeatureGateNotice.test.tsx:
 *
 * 1. `pending` and `allowed` render NOTHING — no skeleton, no placeholder.
 *    A pending gate is what a still-loading (or failing) tier matrix looks
 *    like; it must never flash an upsell at a tenant that is entitled. A
 *    page whose whole content waits on the gate renders its own loader for
 *    `pending`.
 * 2. An upgrade CTA renders only when `gate.canUpgrade === true` and the
 *    state is `plan` or `quota`. `deployment`, `role` and `allowlist` have no
 *    `action` message at all, so no CTA can be rendered for them whatever
 *    `canUpgrade` says. (A `plan` gate that no tier lifts has no tier to
 *    name, so it has no CTA either.)
 * 3. The messages are formatted with the five ICU arguments `{feature}`,
 *    `{plan}`, `{currentPlan}`, `{current}` and `{limit}` only — and each only
 *    when the descriptor carries it. `{plan}` / `{currentPlan}` are the
 *    RESOLVED labels, never a raw plan key.
 * 4. `scope` defaults to `"all"`. The `"create"` copy promises that existing
 *    objects keep working; printed to a tenant that has none it is a false
 *    promise, so it is opt-in.
 *
 * Toast is NOT a variant. A rendered component cannot be called from the
 * `catch` block where a server refusal arrives, and a variant that fires a
 * toast from an effect would make rendering own a side effect. Use the
 * `featureGateToast` export instead; it selects its messages through the
 * same `gateMessageKeys`, so a toast and a banner on one page cannot drift.
 */

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  Info,
  Lock,
  ShieldAlert,
  type LucideIcon,
} from "lucide-react";

import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { WorkspaceRole } from "@/lib/auth/rbac";
import {
  isBlocked,
  type FeatureGate,
  type RefusedGateState,
} from "@/lib/gates/featureGates";
import { cn } from "@/lib/utils/cn";

/** Where every upgrade CTA goes. Only rendered when `gate.canUpgrade`. */
const PLAN_PAGE_HREF = "/workspace/settings/plan";

export type FeatureGateVariant = "inline" | "page" | "control";

/**
 * Is the refusal about CREATING new objects (existing ones keep working —
 * #1551's "gates block new") or about the feature as a whole? Defaults to
 * `"all"`, the copy that promises less.
 */
export type FeatureGateScope = "create" | "all";

type RoleKeyRole = WorkspaceRole.Owner | WorkspaceRole.Admin;

/** Every message under `gate.*` a notice can render, relative to `gate`. */
export type GateMessageKey =
  | "plan.title"
  | "plan.description"
  | "plan.newTitle"
  | "plan.newDescription"
  | "plan.titleNoTier"
  | "plan.descriptionNoTier"
  | "plan.action"
  | "plan.badge"
  | "plan.hint"
  | "plan.hintNoTier"
  | "quota.title"
  | "quota.description"
  | "quota.descriptionNoPlan"
  | "quota.descriptionNoNumbers"
  | "quota.upsell"
  | "quota.action"
  | "quota.badge"
  | "quota.hint"
  | "deployment.title"
  | "deployment.description"
  | "deployment.badge"
  | "deployment.hint"
  | "role.owner.title"
  | "role.owner.description"
  | "role.owner.badge"
  | "role.owner.hint"
  | "role.admin.title"
  | "role.admin.description"
  | "role.admin.badge"
  | "role.admin.hint"
  | "allowlist.title"
  | "allowlist.description"
  | "allowlist.badge"
  | "allowlist.hint";

/**
 * Which `gate.features.<key>` sub-key a message interpolates as `{feature}`:
 * `plural` is lowercase mid-sentence ("The L plan includes resources"),
 * `singular` is lowercase and modifies a noun ("the context limit"). `null`
 * means the message takes no `{feature}` at all. A property of the MESSAGE,
 * declared once here — never chosen at a call site. The test keeps it in
 * step with the catalogues.
 *
 * `{feature}` is one pre-chosen string ICU cannot inflect, so no English
 * message makes it the subject of a verb or puts it beside a number: the
 * Title-cased `label` as a subject reads "Connectors is available", and a
 * noun beside `{limit}` reads "includes 1 contexts". No message takes
 * `label` for that reason; it stays in the catalogue as the product term the
 * parity test checks the nouns against. The quota messages take `singular`
 * and keep the noun off the numbers ("context limit is 1, and current usage
 * is 1").
 */
export const GATE_NOUN_FORM: Readonly<
  Record<GateMessageKey, "label" | "plural" | "singular" | null>
> = {
  "plan.title": "plural",
  "plan.description": "plural",
  "plan.newTitle": "plural",
  "plan.newDescription": "plural",
  "plan.titleNoTier": "plural",
  "plan.descriptionNoTier": "plural",
  "plan.action": null,
  "plan.badge": null,
  "plan.hint": null,
  "plan.hintNoTier": null,
  "quota.title": "singular",
  "quota.description": "singular",
  "quota.descriptionNoPlan": "singular",
  "quota.descriptionNoNumbers": "singular",
  "quota.upsell": null,
  "quota.action": null,
  "quota.badge": null,
  "quota.hint": null,
  "deployment.title": "plural",
  "deployment.description": "plural",
  "deployment.badge": null,
  "deployment.hint": null,
  "role.owner.title": null,
  "role.owner.description": "plural",
  "role.owner.badge": null,
  "role.owner.hint": null,
  "role.admin.title": null,
  "role.admin.description": "plural",
  "role.admin.badge": null,
  "role.admin.hint": null,
  "allowlist.title": "plural",
  "allowlist.description": "plural",
  "allowlist.badge": null,
  "allowlist.hint": null,
};

/** The messages one refusal renders. `null` = that slot does not exist. */
export interface GateMessageKeys {
  readonly title: GateMessageKey;
  readonly description: GateMessageKey;
  /** Quota only: the sentence naming the tier that raises the cap. */
  readonly upsell: GateMessageKey | null;
  /** The CTA label. `null` = this refusal never has a CTA. */
  readonly action: GateMessageKey | null;
  /** The short pill beside a control. */
  readonly badge: GateMessageKey | null;
  /** The one-line explanation under a disabled control. */
  readonly hint: GateMessageKey;
}

const ROLE_KEYS: Readonly<Record<RoleKeyRole, GateMessageKeys>> = {
  [WorkspaceRole.Owner]: {
    title: "role.owner.title",
    description: "role.owner.description",
    upsell: null,
    action: null,
    badge: "role.owner.badge",
    hint: "role.owner.hint",
  },
  [WorkspaceRole.Admin]: {
    title: "role.admin.title",
    description: "role.admin.description",
    upsell: null,
    action: null,
    badge: "role.admin.badge",
    hint: "role.admin.hint",
  },
};

/**
 * Which `gate.*` messages a refusal renders. Pure, and the ONE selection
 * every variant and the toast share.
 *
 * Each branch picks only messages it can fill: `hasRequiredPlan` gates every
 * `{plan}`, `hasCurrentPlan` the `{currentPlan}`, `hasNumbers` the
 * `{current}` / `{limit}`. A message whose argument is missing makes
 * next-intl raise FORMATTING_ERROR and render the raw key instead.
 */
export function gateMessageKeys(
  state: RefusedGateState,
  scope: FeatureGateScope,
  requiredRole: RoleKeyRole | undefined,
  hasRequiredPlan: boolean,
  hasCurrentPlan: boolean,
  /** `gate.current !== undefined && gate.limit !== undefined` */
  hasNumbers: boolean,
): GateMessageKeys {
  switch (state) {
    case "plan":
      if (!hasRequiredPlan) {
        // No served tier has the feature, so there is no tier to name: every
        // message here takes {feature} only, and there is nothing to upgrade
        // to, so no action and no badge.
        return {
          title: "plan.titleNoTier",
          description: "plan.descriptionNoTier",
          upsell: null,
          action: null,
          badge: null,
          hint: "plan.hintNoTier",
        };
      }
      return scope === "create"
        ? {
            title: "plan.newTitle",
            description: "plan.newDescription",
            upsell: null,
            action: "plan.action",
            badge: "plan.badge",
            hint: "plan.hint",
          }
        : {
            title: "plan.title",
            description: "plan.description",
            upsell: null,
            action: "plan.action",
            badge: "plan.badge",
            hint: "plan.hint",
          };
    case "quota":
      return {
        title: "quota.title",
        // QUOTA-002 and the rate-limit refusals carry no counts.
        description: !hasNumbers
          ? "quota.descriptionNoNumbers"
          : hasCurrentPlan
            ? "quota.description"
            : "quota.descriptionNoPlan",
        upsell: hasRequiredPlan ? "quota.upsell" : null,
        action: "quota.action",
        badge: "quota.badge",
        hint: "quota.hint",
      };
    case "deployment":
      return {
        title: "deployment.title",
        description: "deployment.description",
        upsell: null,
        action: null,
        badge: "deployment.badge",
        hint: "deployment.hint",
      };
    case "role":
      // No known minimum role: fail to the STRICTER (owner) copy.
      return (
        ROLE_KEYS[requiredRole ?? WorkspaceRole.Owner] ??
        ROLE_KEYS[WorkspaceRole.Owner]
      );
    case "allowlist":
      return {
        title: "allowlist.title",
        description: "allowlist.description",
        upsell: null,
        action: null,
        badge: "allowlist.badge",
        hint: "allowlist.hint",
      };
  }
}

/**
 * The `gate`-namespace translator: `useTranslations("gate")` in a component,
 * `createTranslator({ …, namespace: "gate" })` outside one. Keys are relative
 * to `gate` ("plan.title", "features.resources.label").
 */
export type GateTranslator = (
  key: string,
  values?: Record<string, string | number>,
) => string;

/** A refusal's copy, formatted. */
export interface FeatureGateText {
  readonly title: string;
  /**
   * The body copy. On a quota gate the member may upgrade, it ends with the
   * sentence naming the tier that raises the cap.
   */
  readonly description: string;
  /** The CTA label — non-null exactly when a CTA may be rendered. */
  readonly action: string | null;
  /** The pill beside a control; `null` when this refusal has none. */
  readonly badge: string | null;
  /** The one-line explanation under a disabled control. */
  readonly hint: string;
}

/** CJK sentences end in a full-width stop and take no space after it. */
function joinSentences(first: string, second: string): string {
  return /[。！？]$/.test(first) ? `${first}${second}` : `${first} ${second}`;
}

/**
 * Format a refusal's copy with `t = useTranslations("gate")`. Returns `null`
 * for a gate that is not blocked (`pending` / `allowed`), so a caller can
 * write `featureGateText(gate, tGate)?.description ?? t("emptyDesc")`.
 *
 * Only the arguments the descriptor carries are passed: `{feature}` (the
 * noun form the message declares in `GATE_NOUN_FORM`), `{plan}` =
 * `gate.planLabel`, `{currentPlan}` = `gate.currentPlanLabel`, `{current}` /
 * `{limit}` = the counts.
 */
export function featureGateText(
  gate: FeatureGate,
  t: GateTranslator,
  scope: FeatureGateScope = "all",
): FeatureGateText | null {
  if (!isBlocked(gate)) return null;
  const state = gate.state as RefusedGateState;

  const hasRequiredPlan = gate.planLabel !== undefined;
  const hasCurrentPlan = gate.currentPlanLabel !== undefined;
  const hasNumbers = gate.current !== undefined && gate.limit !== undefined;
  const keys = gateMessageKeys(
    state,
    scope,
    gate.requiredRole,
    hasRequiredPlan,
    hasCurrentPlan,
    hasNumbers,
  );

  const values: Record<string, string | number> = {};
  if (gate.planLabel !== undefined) values.plan = gate.planLabel;
  if (gate.currentPlanLabel !== undefined) {
    values.currentPlan = gate.currentPlanLabel;
  }
  if (gate.current !== undefined && gate.limit !== undefined) {
    values.current = gate.current;
    values.limit = gate.limit;
  }
  const format = (key: GateMessageKey): string => {
    const form = GATE_NOUN_FORM[key];
    return t(
      key,
      form === null
        ? values
        : { ...values, feature: t(`features.${gate.feature}.${form}`) },
    );
  };

  const description = format(keys.description);
  // Never promise an upgrade the reader cannot act on.
  const upsell =
    keys.upsell !== null && gate.canUpgrade === true
      ? format(keys.upsell)
      : null;
  const mayOfferUpgrade =
    gate.canUpgrade === true && (state === "plan" || state === "quota");

  return {
    title: format(keys.title),
    description:
      upsell === null ? description : joinSentences(description, upsell),
    action:
      keys.action !== null && mayOfferUpgrade ? format(keys.action) : null,
    badge: keys.badge === null ? null : format(keys.badge),
    hint: format(keys.hint),
  };
}

/** What `featureGateToast` returns; spread it into `toast()`. */
export interface FeatureGateToast {
  readonly title: string;
  readonly description: string;
  /** `"destructive"` for a quota refusal; unset (the default toast) otherwise. */
  readonly variant?: "destructive";
}

/**
 * The toast form of a gate notice, for a `catch` block:
 *
 *   const toastArgs = featureGateToast(gate, tGate, "create");
 *   if (toastArgs) toast(toastArgs);
 *
 * Same message selection as the rendered variants (`gateMessageKeys`).
 * Returns `null` for a gate that is not blocked. A toast has no CTA.
 */
export function featureGateToast(
  gate: FeatureGate,
  t: GateTranslator,
  scope: FeatureGateScope = "all",
): FeatureGateToast | null {
  const text = featureGateText(gate, t, scope);
  if (text === null) return null;
  return gate.state === "quota"
    ? {
        title: text.title,
        description: text.description,
        variant: "destructive",
      }
    : { title: text.title, description: text.description };
}

const ALERT_VARIANT: Readonly<
  Record<RefusedGateState, "upsell" | "warning" | "default">
> = {
  plan: "upsell",
  quota: "warning",
  deployment: "default",
  role: "default",
  allowlist: "default",
};

const ICON: Readonly<Record<RefusedGateState, LucideIcon>> = {
  plan: Lock,
  quota: AlertTriangle,
  deployment: Info,
  role: ShieldAlert,
  allowlist: Lock,
};

interface FeatureGateNoticeBaseProps {
  /** The descriptor. `pending` / `allowed` render nothing. */
  gate: FeatureGate;
  /** Default `"all"`. `"create"` = existing objects keep working. */
  scope?: FeatureGateScope;
  /**
   * Put on the element a control's `aria-describedby` should point at: the
   * Alert (`inline`), the notice body (`page`), the hint line (`control`).
   */
  id?: string;
  className?: string;
}

export interface FeatureGateInlineProps extends FeatureGateNoticeBaseProps {
  variant?: "inline";
}

export interface FeatureGatePageProps extends FeatureGateNoticeBaseProps {
  variant: "page";
  /**
   * Given → the notice is the whole page: PageContainer + PageHeader with
   * this title (and `pageDescription`) around it. Omitted → a bare
   * EmptyState for use inside an existing page or tab.
   */
  pageTitle?: string;
  pageDescription?: string;
}

export interface FeatureGateControlProps extends FeatureGateNoticeBaseProps {
  variant: "control";
  /** The control being annotated; the badge renders beside it. */
  children?: ReactNode;
  /** Render the badge pill (default `true`). */
  showBadge?: boolean;
  /**
   * The line under the control: the short `hint` (default) or the full
   * `description` sentence.
   */
  detail?: "hint" | "description";
}

export type FeatureGateNoticeProps =
  FeatureGateInlineProps | FeatureGatePageProps | FeatureGateControlProps;

export function FeatureGateNotice(props: FeatureGateNoticeProps) {
  const t = useTranslations("gate");
  const router = useRouter();
  const { gate, scope = "all", id, className } = props;

  const text = featureGateText(gate, t, scope);
  if (text === null) return null;
  const state = gate.state as RefusedGateState;
  const openPlanPage = () => router.push(PLAN_PAGE_HREF);

  if (props.variant === "page") {
    const body = (
      <div id={id} className={className}>
        <EmptyState
          icon={ICON[state]}
          title={text.title}
          description={text.description}
          {...(text.action !== null
            ? { actionLabel: text.action, onAction: openPlanPage }
            : {})}
        />
      </div>
    );
    if (props.pageTitle === undefined) return body;
    return (
      <PageContainer>
        <PageHeader
          title={props.pageTitle}
          description={props.pageDescription}
        />
        {body}
      </PageContainer>
    );
  }

  if (props.variant === "control") {
    const { children, showBadge = true, detail = "hint" } = props;
    const badge = showBadge ? text.badge : null;
    return (
      <div className={cn("space-y-1", className)}>
        {(children !== undefined || badge !== null) && (
          <div className="flex flex-wrap items-center gap-2">
            {children}
            {badge !== null && (
              <Badge variant="outline" className="text-xs">
                {badge}
              </Badge>
            )}
          </div>
        )}
        <p id={id} className="text-xs text-muted-foreground">
          {detail === "description" ? text.description : text.hint}
        </p>
        {text.action !== null && (
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={openPlanPage}
          >
            {text.action}
          </Button>
        )}
      </div>
    );
  }

  const Icon = ICON[state];
  return (
    <Alert
      id={id}
      variant={ALERT_VARIANT[state]}
      className={cn("mb-4", className)}
    >
      <Icon className="h-4 w-4" />
      <AlertTitle>{text.title}</AlertTitle>
      <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
        <span>{text.description}</span>
        {text.action !== null && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={openPlanPage}
          >
            {text.action}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
