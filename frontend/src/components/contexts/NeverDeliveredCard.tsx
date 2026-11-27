/**
 * NeverDeliveredCard (#3022)
 *
 * Overview-tab card with the number of the viewer's own memories in this
 * context that were never delivered (older than the server's threshold,
 * content never returned by a search or opened). It is an extra: while
 * loading, on an error and at zero it renders nothing.
 *
 * The Review link is a FRESH query string on the current path, never a merge
 * of the current parameters: an earlier Memories visit leaves `q`, `tags` and
 * a memory id in the URL, and a merged link would open a list whose total
 * differs from the count on the card.
 */

"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Archive } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  getNeverDeliveredCount,
  type NeverDeliveredCount,
} from "@/lib/api/contexts";

export const NEVER_DELIVERED_PARAM = "never_delivered";
const REVIEW_QUERY = `?tab=memories&${NEVER_DELIVERED_PARAM}=1`;

interface NeverDeliveredCardProps {
  contextId: string;
  /** The viewer can open the Memories tab (workspace Admin or above). */
  canOpenMemories: boolean;
  /** Bumped by the Overview tab's Refresh button: a change fetches again. */
  refreshKey: number;
}

export function NeverDeliveredCard({
  contextId,
  canOpenMemories,
  refreshKey,
}: NeverDeliveredCardProps) {
  const t = useTranslations("neverDelivered");
  const pathname = usePathname();
  const [data, setData] = useState<NeverDeliveredCount | null>(null);

  useEffect(() => {
    let cancelled = false;
    getNeverDeliveredCount(contextId).then(
      (response) => {
        if (!cancelled) setData(response);
      },
      () => {
        // An extra, not a panel: a failed read shows nothing.
        if (!cancelled) setData(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [contextId, refreshKey]);

  if (!data || data.count <= 0) return null;

  const tags = data.exempt_tags.join(", ");
  return (
    <Card data-testid="never-delivered-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Archive className="h-5 w-5" aria-hidden="true" />
          {t("cardTitle")}
        </CardTitle>
        <CardDescription>
          {t("cardBody", { count: data.count, days: data.older_than_days })}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          {data.exempt_tags.length > 0
            ? t("cardExcludedWithTags", { tags })
            : t("cardExcluded")}
        </p>
        {canOpenMemories ? (
          <Button asChild variant="outline" size="sm">
            <Link href={`${pathname}${REVIEW_QUERY}`}>{t("cardReview")}</Link>
          </Button>
        ) : (
          <p className="text-sm text-muted-foreground">{t("cardNoAccess")}</p>
        )}
      </CardContent>
    </Card>
  );
}
