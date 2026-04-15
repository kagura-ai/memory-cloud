import { type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils/cn";

type KpiTone = "primary" | "secondary" | "muted";

interface KpiCardProps {
  icon: LucideIcon;
  label: string;
  value: string | number;
  subtext?: string;
  /**
   * Optional `title` attribute on the value (used by the Resource detail
   * stats strip to surface the absolute timestamp on hover, while the
   * displayed value stays human-relative). Same string is passed through
   * unchanged, so callers control formatting and timezone.
   */
  valueTitle?: string;
  /**
   * Visual weight. ``primary`` is the headline metric a user actually
   * watches, ``secondary`` is supporting context, and ``muted`` is for
   * cards reporting a benign null/empty ("—" / "not registered") so the
   * grid stops competing with the headline. Tone affects color and
   * weight only — value font size stays constant across tones so cards
   * sitting next to each other in a grid line up.
   */
  tone?: KpiTone;
}

export function KpiCard({
  icon: Icon,
  label,
  value,
  subtext,
  valueTitle,
  tone = "primary",
}: KpiCardProps) {
  const containerCls = cn(
    "p-4 bg-white dark:bg-gray-800 border rounded-lg flex flex-col transition-colors",
    tone === "muted"
      ? "border-gray-200/60 dark:border-gray-700/60 bg-gray-50/40 dark:bg-gray-800/40"
      : "border-gray-200 dark:border-gray-700",
  );

  const iconCls = cn(
    "h-4 w-4",
    tone === "primary" ? "text-foreground/70" : "text-gray-400",
  );

  const valueCls = cn(
    "text-2xl font-semibold leading-tight",
    tone === "primary"
      ? "text-gray-900 dark:text-gray-100"
      : tone === "secondary"
        ? "text-gray-800 dark:text-gray-200 font-medium"
        : "text-gray-400 dark:text-gray-500 font-medium",
  );

  return (
    <div className={containerCls}>
      <div className="flex items-center gap-2 mb-1">
        <Icon className={iconCls} />
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {label}
        </span>
      </div>
      <p className={valueCls} title={valueTitle}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
      {/* Reserve the subtext line in every card so cards in a grid keep the
          same height regardless of whether they have a subtext to show.
          ``aria-hidden`` keeps the placeholder out of the accessibility tree. */}
      <p
        className="text-xs text-muted-foreground mt-1 min-h-[1rem]"
        aria-hidden={!subtext || undefined}
      >
        {subtext ?? "\u00a0"}
      </p>
    </div>
  );
}
