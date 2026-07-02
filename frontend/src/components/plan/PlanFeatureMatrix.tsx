"use client";

/**
 * PlanFeatureMatrix (#1138)
 *
 * Per-tier capability comparison (free / basic / pro) for the owner Plan page.
 * Source of truth = backend `GET /api/v1/workspaces/plan-tiers` (curated from
 * `config/plan_tiers.py`, env-overridable). **No price column** — pricing lives
 * on the payment side (#1141 / #1096); this surface is feature limits only.
 *
 * Numeric `0` renders as ✗ ("not available on this tier"); booleans render
 * ✓ / ✗. The caller's current tier column is highlighted.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { useLocale } from "@/i18n";
import { planLabelFromEnv, type PlanTier } from "@/lib/utils/planLabel";
import { getPlanTierMatrix, type PlanTierFeature } from "@/lib/api/workspaces";

type RowKind = "number" | "bytes" | "bool";

interface MatrixRow {
  key: string;
  field: keyof PlanTierFeature;
  kind: RowKind;
  beta?: boolean; // tag this capability as Beta (badge next to the row label)
}

const ROWS: MatrixRow[] = [
  { key: "contexts", field: "max_contexts", kind: "number" },
  { key: "members", field: "max_members", kind: "number" },
  { key: "memories", field: "memory_limit", kind: "number" },
  { key: "storage", field: "storage_limit_bytes", kind: "bytes" },
  { key: "mcpPerDay", field: "mcp_calls_per_day", kind: "number" },
  { key: "restPerDay", field: "rest_calls_per_day", kind: "number" },
  { key: "publicPerDay", field: "public_calls_per_day", kind: "number" },
  { key: "resourceTokens", field: "max_resource_tokens", kind: "number" },
  { key: "connectors", field: "max_connectors", kind: "number", beta: true },
  {
    key: "analysisPerDay",
    field: "analysis_runs_per_day",
    kind: "number",
    beta: true,
  },
  {
    key: "sleepContexts",
    field: "sleep_enabled_contexts_limit",
    kind: "number",
    beta: true,
  },
  { key: "reranking", field: "reranking", kind: "bool" },
  { key: "managedEmbeddings", field: "managed_embeddings", kind: "bool" },
  { key: "secretStore", field: "secret_store", kind: "bool", beta: true },
  { key: "sharedContexts", field: "shared_contexts", kind: "bool" },
  { key: "teamInvitations", field: "team_invitations", kind: "bool" },
];

const TIER_KEYS = new Set(["free", "basic", "pro"]);

// GiB/MiB storage, matching the admin plan-tiers convention. The shared
// `formatBytes` util renders MB/GB, which diverges from the GiB convention
// used for plan quotas (see admin/plans/_addon-types.ts).
function formatStorage(bytes: number): string {
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) {
    return Number.isInteger(gib) ? `${gib} GiB` : `${gib.toFixed(1)} GiB`;
  }
  const mib = bytes / 1024 ** 2;
  if (mib >= 1) {
    return Number.isInteger(mib) ? `${mib} MiB` : `${mib.toFixed(0)} MiB`;
  }
  return `${bytes.toLocaleString()} B`;
}

export function PlanFeatureMatrix({
  currentTier,
}: {
  currentTier?: string | null;
}) {
  const t = useTranslations("workspace");
  const { locale } = useLocale();
  const [tiers, setTiers] = useState<PlanTierFeature[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getPlanTierMatrix()
      .then((data) => {
        if (alive) setTiers(data);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (error) return <ErrorBanner error={error} />;
  if (!tiers) return <TableLoadingState rows={6} />;

  const tierLabel = (name: string, display: string) =>
    TIER_KEYS.has(name) ? planLabelFromEnv(name as PlanTier, locale) : display;

  const no = (
    <span
      className="text-gray-300 dark:text-gray-600"
      aria-label={t("planMatrix.notIncluded")}
    >
      ✗
    </span>
  );

  const renderCell = (row: MatrixRow, tier: PlanTierFeature) => {
    const value = tier[row.field];
    if (row.kind === "bool") {
      return value ? (
        <span
          className="text-green-600 dark:text-green-400"
          aria-label={t("planMatrix.included")}
        >
          ✓
        </span>
      ) : (
        no
      );
    }
    const n = value as number;
    if (n === 0) return no;
    return row.kind === "bytes" ? formatStorage(n) : n.toLocaleString(locale);
  };

  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("planMatrix.featureColumn")}</TableHead>
            {tiers.map((tier) => (
              <TableHead
                key={tier.name}
                className={`text-center ${
                  tier.name === currentTier ? "font-bold text-primary" : ""
                }`}
              >
                {tierLabel(tier.name, tier.display_name)}
                {tier.name === currentTier && (
                  <span className="ml-1 text-xs font-normal text-gray-500">
                    ({t("planMatrix.current")})
                  </span>
                )}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {ROWS.map((row) => (
            <TableRow key={row.key}>
              <TableCell className="text-sm font-medium">
                <span className="inline-flex items-center gap-2">
                  {t(`planMatrix.row_${row.key}`)}
                  {row.beta && (
                    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
                      {t("planMatrix.beta")}
                    </span>
                  )}
                </span>
              </TableCell>
              {tiers.map((tier) => (
                <TableCell
                  key={tier.name}
                  className={`text-center text-sm ${
                    tier.name === currentTier
                      ? "bg-gray-50 dark:bg-gray-800/40"
                      : ""
                  }`}
                >
                  {renderCell(row, tier)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
