"use client";

/**
 * Workspace Settings > Plan (#1121)
 *
 * Owner-facing plan view + billing handoff. Consolidates the previously
 * scattered upgrade prompts (the members "Pro plan required" modal, the
 * contexts/resources quota CTAs) into one page; those gates now route here.
 *
 * - Read-only plan/entitlement for every workspace member.
 * - "Manage billing" routes the OWNER through the signed billing handoff
 *   (#1118) to the external billing service. memory-cloud stays Stripe-agnostic
 *   (#1096) — there is NO self-serve plan mutation here. When billing is
 *   unconfigured the handoff returns 503 and we surface "not available".
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Sparkles } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { Section } from "@/components/common/Section";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { getWorkspacePlan, type WorkspacePlanInfo } from "@/lib/api/workspaces";
import { mintBillingHandoff } from "@/lib/api/billing";
import { ApiError } from "@/lib/api/base";
import { useLocale } from "@/i18n";
import { planLabelFromEnv, type PlanTier } from "@/lib/utils/planLabel";

export default function WorkspacePlanPage() {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const { locale } = useLocale();
  const { currentWorkspaceId, currentWorkspace } = useWorkspace();
  const { toast } = useToast();

  const [plan, setPlan] = useState<WorkspacePlanInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [upgrading, setUpgrading] = useState(false);

  const isOwner = currentWorkspace?.current_user_role === "owner";

  const loadPlan = useCallback(async () => {
    if (!currentWorkspaceId) return;
    try {
      setLoading(true);
      setError(null);
      setPlan(await getWorkspacePlan(currentWorkspaceId));
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setLoading(false);
    }
  }, [currentWorkspaceId]);

  useEffect(() => {
    loadPlan();
  }, [loadPlan]);

  const handleManageBilling = async () => {
    if (!currentWorkspaceId) return;
    try {
      setUpgrading(true);
      const res = await mintBillingHandoff(currentWorkspaceId);
      if (res.url) {
        window.location.href = res.url;
        return;
      }
      // Token minted but no ready-to-use URL (PAYMENT_PUBLIC_BASE_URL unset):
      // there is nowhere to send the owner from this deployment.
      toast({
        variant: "destructive",
        title: tCommon("error"),
        description: t("planPage.unavailable"),
      });
    } catch (e) {
      // 503 (BILLING-002) = handoff signing key unconfigured → billing disabled.
      const description =
        e instanceof ApiError && e.status === 503
          ? t("planPage.unavailable")
          : e instanceof Error
            ? e.message
            : t("planPage.upgradeError");
      toast({ variant: "destructive", title: tCommon("error"), description });
    } finally {
      setUpgrading(false);
    }
  };

  if (loading) {
    return <SpinnerLoading size="lg" message={tCommon("loading")} />;
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader
          title={t("planPage.title")}
          description={t("planPage.description")}
        />
        <ErrorBanner error={error.message} />
      </PageContainer>
    );
  }

  // Label precedence: localized tier label (OSS default S/M/L, per-locale
  // override via env) → backend display_name → raw plan_name → em dash.
  const canonicalTier = currentWorkspace?.plan_name;
  const planName =
    canonicalTier === "free" ||
    canonicalTier === "basic" ||
    canonicalTier === "pro"
      ? planLabelFromEnv(canonicalTier as PlanTier, locale)
      : (plan?.plan_display_name ?? canonicalTier ?? "—");

  // "Subscribed" = on a paid tier. memory-cloud is price/currency-agnostic
  // (#1096 — payment is the billing SoT; entitlement push carries plan_name +
  // addons only, no price/currency), so the actual billed amount/currency is
  // confirmed in the billing portal, never hardcoded here (#1141).
  const subscribedTier = canonicalTier ?? plan?.current_plan;
  const isSubscribed = subscribedTier === "basic" || subscribedTier === "pro";

  return (
    <PageContainer>
      <PageHeader
        title={t("planPage.title")}
        description={t("planPage.description")}
      />

      <Section title={t("planPage.currentPlan")}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-2xl font-semibold capitalize">{planName}</p>
            {isSubscribed && (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {t("planPage.billingAmountHint")}
              </p>
            )}
          </div>
          {isOwner ? (
            <Button onClick={handleManageBilling} disabled={upgrading}>
              <Sparkles className="mr-2 h-4 w-4" />
              {upgrading
                ? t("planPage.opening")
                : isSubscribed
                  ? t("planPage.reviewOrChangePlan")
                  : t("planPage.manageBilling")}
            </Button>
          ) : (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {t("planPage.ownerOnly")}
            </p>
          )}
        </div>
      </Section>

      {plan && (
        <Section title={t("planPage.usageTitle")}>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <div className="flex justify-between border-b border-gray-100 pb-1 dark:border-gray-800">
              <dt className="text-gray-500 dark:text-gray-400">
                {t("planPage.memories")}
              </dt>
              <dd className="font-medium">
                {plan.usage.memories.toLocaleString()} /{" "}
                {plan.quotas.memory_limit.toLocaleString()}
              </dd>
            </div>
            <div className="flex justify-between border-b border-gray-100 pb-1 dark:border-gray-800">
              <dt className="text-gray-500 dark:text-gray-400">
                {t("planPage.contexts")}
              </dt>
              <dd className="font-medium">
                {plan.usage.contexts.toLocaleString()} /{" "}
                {plan.quotas.max_contexts.toLocaleString()}
              </dd>
            </div>
            <div className="flex justify-between border-b border-gray-100 pb-1 dark:border-gray-800">
              <dt className="text-gray-500 dark:text-gray-400">
                {t("planPage.mcpPerDay")}
              </dt>
              <dd className="font-medium">
                {plan.quotas.mcp_calls_per_day.toLocaleString()}
              </dd>
            </div>
            <div className="flex justify-between border-b border-gray-100 pb-1 dark:border-gray-800">
              <dt className="text-gray-500 dark:text-gray-400">
                {t("planPage.restPerDay")}
              </dt>
              <dd className="font-medium">
                {plan.quotas.rest_calls_per_day.toLocaleString()}
              </dd>
            </div>
            <div className="flex justify-between border-b border-gray-100 pb-1 dark:border-gray-800">
              <dt className="text-gray-500 dark:text-gray-400">
                {t("planPage.publicPerDay")}
              </dt>
              <dd className="font-medium">
                {plan.quotas.public_calls_per_day.toLocaleString()}
              </dd>
            </div>
          </dl>
        </Section>
      )}

      {plan?.can_upgrade && (
        <Section title={t("proPlanBenefits")}>
          <ul className="space-y-1 text-sm text-gray-700 dark:text-gray-300">
            <li>✓ {t("benefitTeamInvitations")}</li>
            <li>✓ {t("benefitSharedContexts")}</li>
            <li>✓ {t("benefitCollaboration")}</li>
          </ul>
        </Section>
      )}
    </PageContainer>
  );
}
