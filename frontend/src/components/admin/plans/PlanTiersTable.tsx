"use client";

/**
 * Plan tier comparison (Issue #664).
 *
 * Moved out of the Quotas page's Tiers tab into the admin Users page's Plan
 * tiers category in #2765. Live tier configuration from
 * `GET /api/v1/admin/plans/tiers` (environment overrides applied), with an
 * info card on how add-ons and the zero floor work.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Inbox, Info } from "lucide-react";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { getAdminPlanTiers, type PlanTierInfo } from "@/lib/api/admin";

// ============================================================================
// Tier comparison table — row definitions (Issue #664)
// ============================================================================

// Backend feature-flag identifiers (mirrors `PlanTier.features` strings in
// `backend/src/config/plan_tiers.py`). Kept as a `const` map so the row
// definitions reference symbolic names instead of bare strings — typos are
// caught at compile time and the mapping between row and feature is auditable.
const TIER_FEATURES = {
  RERANKING: "reranking",
  OAUTH: "oauth",
  MEMORY_ANALYSIS: "memory_analysis",
} as const;

type TierRowDef = {
  readonly key: string;
  readonly render: (tier: PlanTierInfo) => string;
};

const formatNumber = (value: number): string =>
  value === 0 ? "—" : value.toLocaleString();
const formatBool = (value: boolean): string => (value ? "✅" : "—");
const hasFeature = (tier: PlanTierInfo, name: string): string =>
  formatBool(tier.features.includes(name));
const formatStorage = (bytes: number): string => {
  if (bytes === 0) return "—";
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) {
    return Number.isInteger(gib) ? `${gib} GiB` : `${gib.toFixed(1)} GiB`;
  }
  return `${Math.round(bytes / 1024 ** 2)} MiB`;
};

// `as const satisfies` preserves the literal `key` types while still
// constraining each entry's shape.
const TIER_ROW_DEFINITIONS = [
  {
    key: "contextsPerWorkspace",
    render: (t: PlanTierInfo) => formatNumber(t.max_contexts_per_workspace),
  },
  {
    key: "memories",
    render: (t: PlanTierInfo) => formatNumber(t.memory_limit),
  },
  {
    // Issue #1549: memories created per UTC day. An older API omits the
    // field (rolling deploy) → "—" like a zero quota, not a crash.
    key: "memoriesPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.memories_per_day ?? 0),
  },
  {
    key: "mcpCallsPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.mcp_calls_per_day),
  },
  {
    key: "analysisRuns",
    render: (t: PlanTierInfo) => formatNumber(t.analysis_runs_per_day),
  },
  {
    key: "reranking",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.RERANKING),
  },
  {
    key: "mcpAppCredentials",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.OAUTH),
  },
  {
    key: "storage",
    render: (t: PlanTierInfo) => formatStorage(t.storage_limit_bytes),
  },
  {
    key: "maxMembers",
    render: (t: PlanTierInfo) => formatNumber(t.max_members_per_workspace),
  },
  {
    // #1550: per-user owned-workspace cap on this tier (1 base + grant).
    key: "ownedWorkspaces",
    render: (t: PlanTierInfo) => formatNumber(t.owned_workspaces),
  },
  {
    key: "maxResourceTokens",
    render: (t: PlanTierInfo) => formatNumber(t.max_resource_tokens),
  },
  {
    // #1551: the tier's real cap — serve-only on M/L (existing connectors),
    // the creation cap on XL. The boolean rows below say who may create.
    key: "maxConnectors",
    render: (t: PlanTierInfo) => formatNumber(t.max_connectors),
  },
  {
    key: "restCallsPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.rest_calls_per_day),
  },
  {
    key: "publicCallsPerDay",
    render: (t: PlanTierInfo) => formatNumber(t.public_calls_per_day),
  },
  {
    key: "boundPublicPerMinute",
    render: (t: PlanTierInfo) => formatNumber(t.bound_public_calls_per_minute),
  },
  {
    key: "sleepContextsLimit",
    render: (t: PlanTierInfo) => formatNumber(t.sleep_enabled_contexts_limit),
  },
  {
    key: "sharedContexts",
    render: (t: PlanTierInfo) => formatBool(t.allows_shared_contexts),
  },
  {
    key: "publicContexts",
    render: (t: PlanTierInfo) => formatBool(t.public_contexts),
  },
  {
    key: "resources",
    render: (t: PlanTierInfo) => formatBool(t.resources),
  },
  {
    key: "connectors",
    render: (t: PlanTierInfo) => formatBool(t.connectors),
  },
  {
    key: "memoryAnalysis",
    render: (t: PlanTierInfo) => hasFeature(t, TIER_FEATURES.MEMORY_ANALYSIS),
  },
] as const satisfies readonly TierRowDef[];

export function PlanTiersTable() {
  const t = useTranslations("admin.plans");
  const [tiers, setTiers] = useState<PlanTierInfo[]>([]);
  const [loading, setLoading] = useState(true);
  // A flag, not the message: the fetch must not depend on the translator.
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    getAdminPlanTiers()
      .then((data) => {
        if (active) setTiers(data);
      })
      .catch(() => {
        if (active) setFailed(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("tiersTable.title")}</CardTitle>
        <CardDescription>{t("tiersTable.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <Alert>
          <Info className="h-4 w-4" />
          <AlertTitle>{t("tiersTable.infoCard.title")}</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>{t("tiersTable.infoCard.envOverrideBody")}</p>
            <p>{t("tiersTable.infoCard.addonBody")}</p>
            <p>{t("tiersTable.infoCard.zeroFloorBody")}</p>
          </AlertDescription>
        </Alert>

        <ErrorBanner error={failed ? t("tiersTable.loadError") : null} />

        {loading ? (
          <TableLoadingState rows={TIER_ROW_DEFINITIONS.length} />
        ) : failed ? null : tiers.length === 0 ? (
          // Empty only for a successful-but-empty response: a failure is the
          // ErrorBanner above (one channel per error class).
          <EmptyState
            icon={Inbox}
            title={t("tiersTable.emptyTitle")}
            description={t("tiersTable.emptyDescription")}
            compact
          />
        ) : (
          <TooltipProvider delayDuration={200}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("tiersTable.feature")}</TableHead>
                  {tiers.map((tier) => (
                    <TableHead key={tier.name}>{tier.display_name}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {TIER_ROW_DEFINITIONS.map((row) => (
                  <TableRow key={row.key}>
                    <TableCell className="font-medium">
                      <Tooltip>
                        <TooltipTrigger
                          className="cursor-help text-left underline decoration-dotted underline-offset-4"
                          type="button"
                        >
                          {t(`tiersTable.${row.key}`)}
                        </TooltipTrigger>
                        <TooltipContent>
                          {t(`tiersTable.${row.key}Description`)}
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    {tiers.map((tier) => (
                      <TableCell key={tier.name}>{row.render(tier)}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TooltipProvider>
        )}
      </CardContent>
    </Card>
  );
}
