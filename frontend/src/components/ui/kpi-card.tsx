import { type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils/cn";

type KpiTone = "primary" | "secondary" | "muted";

interface KpiCardProps {
  icon: LucideIcon;
  label: string;
  value: string | number;
  subtext?: string;
  /**
   * Visual weight. Use ``primary`` for the headline metric a user actually
   * watches, ``secondary`` for supporting metrics, and ``muted`` for cards
   * that report a benign null/empty (e.g. "—" / "not registered") so the
   * grid no longer competes with the headline. Default ``primary`` keeps
   * existing call sites unchanged.
   */
  tone?: KpiTone;
}

export function KpiCard({
  icon: Icon,
  label,
  value,
  subtext,
  tone = "primary",
}: KpiCardProps) {
  const containerCls = cn(
    "p-4 bg-white dark:bg-gray-800 border rounded-lg transition-colors",
    tone === "muted"
      ? "border-gray-200/60 dark:border-gray-700/60 bg-gray-50/40 dark:bg-gray-800/40"
      : "border-gray-200 dark:border-gray-700",
  );

  const iconCls = cn(
    "h-4 w-4",
    tone === "primary" ? "text-foreground/70" : "text-gray-400",
  );

  const valueCls = cn(
    "font-semibold text-gray-900 dark:text-gray-100",
    tone === "primary" ? "text-3xl" : "text-xl",
    tone === "muted" && "text-gray-400 dark:text-gray-500",
  );

  return (
    <div className={containerCls}>
      <div className="flex items-center gap-2 mb-1">
        <Icon className={iconCls} />
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {label}
        </span>
      </div>
      <p className={valueCls}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
      {subtext && (
        <p className="text-xs text-muted-foreground mt-1">{subtext}</p>
      )}
    </div>
  );
}
