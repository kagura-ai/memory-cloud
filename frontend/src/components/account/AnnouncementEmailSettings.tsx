"use client";

/**
 * Announcement emails in account settings (#2761).
 *
 * A toggle for "send me occasional emails about new features and
 * announcements". Every change is recorded by the backend with its source;
 * service notices about the account (security, password, invitations) are not
 * affected and the card says so.
 *
 * While the deployment does not offer announcement emails the card is hidden,
 * unless the setting is still on: then the toggle stays so the person can turn
 * it off (withdrawal never depends on configuration).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Mail } from "lucide-react";

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
  getAnnouncementEmails,
  setAnnouncementEmails,
  type AnnouncementEmailsSetting,
} from "@/lib/api/announcementEmails";
import { formatDateTime } from "@/lib/utils/datetime";

export function AnnouncementEmailSettings({
  onVisibilityChange,
}: {
  /** Told whether the card shows, so the page can list it in its navigation. */
  onVisibilityChange?: (visible: boolean) => void;
} = {}) {
  const t = useTranslations("profile.announcementEmails");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const { locale } = useLocale();
  const { user } = useAuth();
  const [setting, setSetting] = useState<AnnouncementEmailsSetting | null>(
    null,
  );
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setSetting(await getAnnouncementEmails());
      setLoadError(null);
    } catch {
      setLoadError(t("loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    let cancelled = false;
    getAnnouncementEmails().then(
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

  // Hidden while the deployment does not offer the emails and the setting is
  // off. The page lists the card only once it is known to show (loaded, or
  // failed to load), so a deployment without the feature never flashes it.
  const hidden =
    setting !== null && !setting.offered && !setting.announcement_emails;
  const listed = setting !== null ? !hidden : loadError !== null;
  useEffect(() => {
    onVisibilityChange?.(listed);
  }, [listed, onVisibilityChange]);

  const handleChange = async (value: boolean) => {
    if (!setting || saving) return;
    setSaving(true);
    try {
      const updated = await setAnnouncementEmails(
        value,
        setting.wording_version,
      );
      setSetting(updated);
      toast({ title: value ? t("saved") : t("savedOff") });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // The wording changed (or the deployment stopped offering them):
        // show the current state before the person tries again.
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
            <Mail className="h-5 w-5" />
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
    <Card data-testid="announcement-email-settings">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Mail className="h-5 w-5" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between gap-4">
          <Label htmlFor="announcement-emails">{t("toggle")}</Label>
          <Switch
            id="announcement-emails"
            checked={setting.announcement_emails}
            disabled={
              saving || (!setting.offered && !setting.announcement_emails)
            }
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
