"use client";

import { useTranslations, useLocale } from "next-intl";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { useCanUpgrade } from "@/hooks/useCanUpgrade";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { useRouter } from "next/navigation";
import type { QuotaUsage } from "@/lib/api/usage";

// The owner-only seat and the single Free context are full by design: the
// meter shows them with its own plan link, the quota notice leaves them out.
const STRUCTURAL_CAP_METERS = new Set(["members", "contexts"]);

export function isStructuralCap(meter: QuotaUsage): boolean {
  return STRUCTURAL_CAP_METERS.has(meter.id) && meter.limit === 1 && meter.used === 1;
}

function MeterPlanAction() {
  const t = useTranslations("usageStats");
  const canUpgrade = useCanUpgrade();
  const features = useSystemFeatures();
  const router = useRouter();
  if (canUpgrade === true) {
    return (
      <Button variant="link" size="sm" className="h-auto p-0" onClick={() => router.push("/workspace/settings/plan")}>
        {t("reviewPlan")}
      </Button>
    );
  }
  if (canUpgrade === false && features?.plan_page === true) {
    return <p className="text-xs text-muted-foreground">{t("askOwner")}</p>;
  }
  return null;
}

export function QuotaNotice({ meters, onViewUsage }: { meters: QuotaUsage[]; onViewUsage?: () => void }) {
  const t = useTranslations("usageStats");
  const canUpgrade = useCanUpgrade();
  const features = useSystemFeatures();
  const router = useRouter();
  // A zero allowance with usage (objects kept after a downgrade) is over the
  // limit: it has no percentage, so it is named with the no-allowance text.
  const nearing = meters.filter(meter =>
    !isStructuralCap(meter) &&
    meter.scope === "workspace" && meter.unit !== "usd" && meter.used !== null &&
    meter.limit !== null && meter.limit >= 0 &&
    (meter.limit > 0 ? meter.used / meter.limit >= 0.8 : meter.used > 0)
  );
  if (nearing.length === 0) return null;
  const exceeded = nearing.some(meter => meter.used! >= meter.limit!);
  const share = (meter: QuotaUsage) => meter.limit! > 0
    ? Math.floor(meter.used! / meter.limit! * 100) + "%"
    : t("quotaNotIncluded");
  return (
    <Alert className={exceeded ? "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/20" : "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/20"}>
      <AlertTitle>{t(exceeded ? "quotaReached" : "quotaNear")}</AlertTitle>
      <AlertDescription>
        <p className="mt-1 text-sm">
          {nearing.map(meter => t(`quotaLabels.${meter.id}`) + " " + share(meter)).join(" · ")}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {onViewUsage && <Button variant="outline" size="sm" onClick={onViewUsage}>{t("viewAllQuotas")}</Button>}
          {canUpgrade === true && <Button size="sm" onClick={() => router.push("/workspace/settings/plan")}>{t("reviewPlan")}</Button>}
        </div>
        {canUpgrade === false && features?.plan_page === true && <p className="mt-2 text-sm">{t("askOwner")}</p>}
      </AlertDescription>
    </Alert>
  );
}

export function QuotaOverview({ meters }: { meters: QuotaUsage[] }) {
  const t = useTranslations("usageStats");
  const locale = useLocale();
  const format = (value: number, unit: QuotaUsage["unit"]) => {
    if (unit === "usd") return new Intl.NumberFormat(locale, { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(value);
    if (unit === "bytes") {
      const divisor = value >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2;
      return (value / divisor).toLocaleString(locale, { maximumFractionDigits: 1 }) + (divisor === 1024 ** 3 ? " GB" : " MB");
    }
    return value.toLocaleString(locale);
  };
  const groups = [
    { key: "workspace", rows: meters.filter(m => m.scope === "workspace" && m.period === "current") },
    { key: "requests", rows: meters.filter(m => m.scope === "workspace" && m.period !== "current" && m.unit !== "usd") },
    { key: "account", rows: meters.filter(m => m.scope === "account") },
    { key: "apiKey", rows: meters.filter(m => m.scope === "api_key") },
    { key: "budgets", rows: meters.filter(m => m.unit === "usd") },
  ];
  return (
    <div className="space-y-6">
      <QuotaNotice meters={meters} />
      <p className="text-sm text-muted-foreground">{t("quotaScopeHelp")}</p>
      {groups.filter(group => group.rows.length > 0).map(group => (
        <section key={group.key} aria-label={t(`quotaGroups.${group.key}`)}>
          <h3 className="mb-3 text-base font-semibold">{t(`quotaGroups.${group.key}`)}</h3>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {group.rows.map(meter => {
              const percentage = meter.used !== null && meter.limit !== null && meter.limit > 0 ? meter.used / meter.limit * 100 : null;
              return (
                <div key={meter.id} className="space-y-2 rounded-lg border border-slate-200 bg-white p-4 text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100">
                  <h4 className="text-sm font-medium">{t(`quotaLabels.${meter.id}`)}</h4>
                  <p className="text-lg font-semibold tabular-nums">
                    {meter.used === null ? t("quotaLimitOnly") : format(meter.used, meter.unit) + " / "}
                    {meter.limit === null ? t("quotaUnlimited") : format(meter.limit, meter.unit)}
                  </p>
                  {percentage !== null && <Progress aria-label={t(`quotaLabels.${meter.id}`)} value={Math.min(percentage, 100)} className={percentage >= 100 ? "h-2 [&>div]:bg-red-500" : percentage >= 80 ? "h-2 [&>div]:bg-amber-500" : "h-2"} />}
                  <p className="text-xs text-muted-foreground">
                    {meter.limit === 0 ? t("quotaNotIncluded") : percentage === null ? t("quotaNoPercentage") : t("percentUsed", { percent: percentage.toFixed(1) })}
                    {meter.bonus > 0 && " · " + t("quotaBonus", { amount: format(meter.bonus, meter.unit) })}
                  </p>
                  {meter.creation_included === false && <p className="text-xs text-muted-foreground">{t("quotaCreationNotIncluded")}</p>}
                  {meter.id === "members" && <p className="text-xs text-muted-foreground">{t("includesInvitations")}</p>}
                  {isStructuralCap(meter) && <MeterPlanAction />}
                  {meter.id === "bound_key_rate" && <p className="text-xs text-muted-foreground">{t("perKeyHelp")}</p>}
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
