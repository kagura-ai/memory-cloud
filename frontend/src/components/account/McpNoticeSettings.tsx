"use client";

/**
 * Service notices in AI tools, in account settings (#2762).
 *
 * A toggle for "show short notices from the people who run this service at
 * the start of a session in my AI tools". Off by default; every change is
 * recorded by the backend. Account and security notices still go by email and
 * the card says so.
 *
 * While the deployment does not offer the notices the card is hidden, unless
 * the setting is still on: then the toggle stays so the person can turn it off
 * (withdrawal never depends on configuration).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Megaphone } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import { useLocale } from "@/i18n";
import { useAuth } from "@/contexts/AuthContext";
import { ApiError } from "@/lib/api/base";
import {
  getMcpNotices,
  setMcpNotices,
  type McpNoticesSetting,
} from "@/lib/api/mcpNotices";
import { formatDateTime } from "@/lib/utils/datetime";

export function McpNoticeSettings({
  onVisibilityChange,
}: {
  /** Told whether the card shows, so the page can list it in its navigation. */
  onVisibilityChange?: (visible: boolean) => void;
} = {}) {
  const t = useTranslations("profile.mcpNotices");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const { locale } = useLocale();
  const { user } = useAuth();
  const [setting, setSetting] = useState<McpNoticesSetting | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setSetting(await getMcpNotices());
      setLoadError(null);
    } catch {
      setLoadError(t("loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    let cancelled = false;
    getMcpNotices().then(
      (value) => {
        if (cancelled) return;
        setSetting(value);
        setLoadError(null);
      },
      () => {
        if (!cancelled) setLoadError(t("loadFailed"));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [t]);

  // Hidden while the deployment does not offer the notices and the setting is
  // off. The page lists the card only once it is known to show (loaded, or
  // failed to load), so a deployment without the feature never flashes it.
  const hidden = setting !== null && !setting.offered && !setting.mcp_notices;
  const listed = setting !== null ? !hidden : loadError !== null;
  useEffect(() => {
    onVisibilityChange?.(listed);
  }, [listed, onVisibilityChange]);

  const handleChange = async (value: boolean) => {
    if (!setting || saving) return;
    setSaving(true);
    try {
      const updated = await setMcpNotices(value, setting.wording_version);
      setSetting(updated);
      toast({ title: value ? t("saved") : t("savedOff") });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // The wording changed (MCPNOTICE-006) or the deployment stopped
        // offering the notices (MCPNOTICE-005): show the current state
        // before the person tries again.
        await load();
        toast({
          title: tCommon("error"),
          description: t("stale"),
          variant: "destructive",
        });
      } else {
        toast({
          title: tCommon("error"),
          description: t("failed"),
          variant: "destructive",
        });
      }
    } finally {
      setSaving(false);
    }
  };

  if (loadError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Megaphone className="h-5 w-5" />
            {t("title")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <ErrorBanner error={loadError} />
        </CardContent>
      </Card>
    );
  }

  if (!setting) {
    return (
      <Card>
        <CardContent className="pt-6">
          <LoadingState lines={2} />
        </CardContent>
      </Card>
    );
  }

  if (hidden) {
    return null;
  }

  return (
    <Card data-testid="mcp-notice-settings">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Megaphone className="h-5 w-5" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between gap-4">
          <Label htmlFor="mcp-notices">{t("toggle")}</Label>
          <Switch
            id="mcp-notices"
            checked={setting.mcp_notices}
            disabled={saving || (!setting.offered && !setting.mcp_notices)}
            onCheckedChange={(value) => void handleChange(value)}
          />
        </div>
        {!setting.offered && (
          <p className="text-sm text-slate-500">{t("notOffered")}</p>
        )}
        {setting.recorded_at && (
          <p className="text-sm text-slate-500">
            {t("lastChanged", {
              date: formatDateTime(
                setting.recorded_at,
                user?.timezone || "UTC",
                locale,
              ),
            })}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
