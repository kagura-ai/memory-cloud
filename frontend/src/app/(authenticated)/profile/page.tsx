"use client";

/**
 * User Profile Settings Page
 *
 * User profile management, theme settings, and preferences
 * Issue #672: UI Polish & Design Enhancement - Phase 2
 * Issue #223: i18n support
 */

import { useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAuth } from "@/contexts/AuthContext";
import { useLocale, type Locale } from "@/i18n";
import { useToast } from "@/hooks/use-toast";
import { useConsumeSearchParams } from "@/hooks/useConsumeSearchParams";
import { User, Moon, Sun, Save, RefreshCw } from "lucide-react";
import { COMMON_TIMEZONES } from "@/lib/utils/datetime";
import { apiClient, ApiError } from "@/lib/api/base";
import { PageContainer } from "@/components/common/PageContainer";
import ConnectedAccounts from "@/components/auth/ConnectedAccounts";
import { DeleteAccountSection } from "@/components/account/DeleteAccountSection";
import { getSignInMethodLabel, getRefreshProviderName } from "./signInLabels";

export default function ProfilePage() {
  const t = useTranslations("profile");
  const tCommon = useTranslations("common");
  const { user, refetchUser } = useAuth();
  const { toast } = useToast();
  const [isEditMode, setIsEditMode] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Wait for useAuth() to finish its initial /auth/me fetch before
  // surfacing the refreshed=1 / error=refresh_* toast (enabled: !!user).
  // If the consume ran on the first render (where user is still null), the
  // toast would say "Profile refreshed from IdP" instead of "from Google" /
  // "from GitHub". The hook handles the params exactly once and strips them
  // via router.replace (#1382).
  useConsumeSearchParams(
    (params) => {
      const refreshed = params.get("refreshed");
      const linked = params.get("linked");
      const errorCode = params.get("error");
      const isRefreshParam =
        refreshed === "1" || !!errorCode?.startsWith("refresh_");
      // Link outcomes from the link-mode callback (_maybe_link_redirect, #517).
      const isLinkParam =
        linked === "1" ||
        errorCode === "link_failed" ||
        errorCode === "provider_already_linked";
      if (!isRefreshParam && !isLinkParam) return false;

      const provider =
        getRefreshProviderName(user ?? {}, t) ?? t("signInProviderFallback");

      if (refreshed === "1") {
        toast({
          title: t("refreshFromIdPSuccess", { provider }),
          description: t("refreshFromIdPSuccessDesc"),
        });
        refetchUser();
      } else if (errorCode?.startsWith("refresh_")) {
        // Wire-format contract with backend's _maybe_refresh_redirect:
        // any code not in this map falls through to the generic message.
        const errorMessageKey: Record<string, string> = {
          refresh_user_mismatch: "refreshFromIdPErrorMismatch",
          refresh_state_expired: "refreshFromIdPErrorExpired",
        };
        const messageKey =
          errorMessageKey[errorCode] ?? "refreshFromIdPErrorGeneric";
        toast({
          title: tCommon("error"),
          description: t(messageKey, { provider }),
          variant: "destructive",
        });
      } else if (linked === "1") {
        toast({ title: t("linkSuccess") });
        refetchUser();
      } else if (errorCode === "link_failed") {
        toast({
          title: tCommon("error"),
          description: t("linkFailed"),
          variant: "destructive",
        });
      } else {
        // Group check above guarantees this is provider_already_linked.
        toast({
          title: tCommon("error"),
          description: t("linkAlreadyLinked"),
          variant: "destructive",
        });
      }
      return true;
    },
    { enabled: !!user, cleanUrl: "/profile" },
  );

  const handleRefreshFromIdP = async () => {
    const provider = getRefreshProviderName(user ?? {}, t);
    if (!provider) return; // Defensive: button is hidden in this state
    setIsRefreshing(true);
    try {
      const data = await apiClient.post<{
        authorization_url: string;
        state: string;
      }>("/api/v1/me/refresh-oauth", {});
      // Browser-level navigation: OAuth flow expects a full redirect.
      window.location.href = data.authorization_url;
    } catch (error) {
      setIsRefreshing(false);
      const messageKey =
        error instanceof ApiError && error.status === 429
          ? "refreshFromIdPErrorRateLimited"
          : "refreshFromIdPErrorGeneric";
      toast({
        title: tCommon("error"),
        description: t(messageKey, { provider }),
        variant: "destructive",
      });
    }
  };

  const [isDarkMode, setIsDarkMode] = useState(() => {
    if (typeof window !== "undefined") {
      return (
        localStorage.getItem("theme") === "dark" ||
        document.documentElement.classList.contains("dark")
      );
    }
    return false;
  });

  // Profile form state. ``email`` is intentionally derived directly from
  // ``user?.email`` (no local state) so a successful refresh + refetchUser()
  // updates the displayed value without an extra setEmail step. ``name``
  // and ``timezone`` are local state because the form lets the operator
  // edit them; we sync from ``user`` on change so an edit cancellation
  // restores the latest server-side value.
  const [name, setName] = useState(user?.name || "");
  const email = user?.email || "";
  const [timezone, setTimezone] = useState(user?.timezone || "UTC");
  const [locale, setLocale] = useState(user?.locale || "en");
  // i18n provider's locale setter (localStorage + context) — distinct from the
  // local form state `setLocale` above. Saving the profile must drive this so
  // the running UI actually switches language, not just the backend record.
  const { setLocale: applyUiLocale } = useLocale();

  useEffect(() => {
    if (user) {
      setName(user.name || "");
      setTimezone(user.timezone || "UTC");
      setLocale(user.locale || "en");
    }
  }, [user]);

  const handleSaveProfile = async () => {
    try {
      await apiClient.put("/api/v1/users/profile", {
        name,
        timezone,
        locale,
      });

      // Refresh user data to get updated timezone
      await refetchUser();

      // Apply the chosen UI language immediately (localStorage + context), so
      // the interface switches now instead of only persisting to the backend.
      applyUiLocale(locale as Locale);

      toast({
        title: t("profileUpdated"),
        description: t("profileUpdatedDesc"),
      });
      setIsEditMode(false);
    } catch (error: any) {
      toast({
        title: tCommon("error"),
        description: error.message || t("failedToUpdateProfile"),
        variant: "destructive",
      });
    }
  };

  const handleToggleDarkMode = () => {
    const newMode = !isDarkMode;
    setIsDarkMode(newMode);

    // Toggle dark mode class on document
    if (newMode) {
      document.documentElement.classList.add("dark");
      localStorage.setItem("theme", "dark");
    } else {
      document.documentElement.classList.remove("dark");
      localStorage.setItem("theme", "light");
    }

    toast({
      title: t("themeUpdated"),
      description: newMode ? t("themeUpdatedDark") : t("themeUpdatedLight"),
    });
  };

  return (
    <PageContainer>
      {/* Page Header */}
      <div>
        <h1 className="text-3xl font-bold tracking-tight">{t("title")}</h1>
        <p className="text-slate-500 dark:text-slate-400 mt-2">
          {t("subtitle")}
        </p>
      </div>

      {/* Profile Information */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <User className="h-5 w-5" />
              {t("profileInfo")}
            </div>
            {!isEditMode && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setIsEditMode(true)}
              >
                {tCommon("edit")}
              </Button>
            )}
          </CardTitle>
          <CardDescription>{t("profileInfoDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {user && (
            <>
              <div className="space-y-2">
                <Label htmlFor="name">{t("fullName")}</Label>
                <Input
                  id="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={!isEditMode}
                  placeholder={t("fullNamePlaceholder")}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="email">{t("emailAddress")}</Label>
                <Input
                  id="email"
                  type="email"
                  value={email}
                  disabled
                  className="bg-slate-50 dark:bg-slate-900"
                />
                <p className="text-xs text-slate-500">
                  {t("emailCannotBeChanged")}
                </p>
              </div>

              {/* Issue #514: read-only sign-in method display */}
              <div className="space-y-2">
                <Label htmlFor="sign-in-method">{t("signInMethod")}</Label>
                <Input
                  id="sign-in-method"
                  value={getSignInMethodLabel(user, t)}
                  disabled
                  className="bg-slate-50 dark:bg-slate-900"
                />
                <p className="text-xs text-slate-500">
                  {t("signInMethodDesc")}
                </p>
              </div>

              {/* Issue #515: manual IdP refresh — only visible to OAuth
                  users with a known provider. Password users and pre-#361
                  null-provider users see nothing. */}
              {(() => {
                const refreshProvider = getRefreshProviderName(user, t);
                if (!refreshProvider) return null;
                return (
                  <div className="space-y-2 rounded-md border border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/30 p-3">
                    {/* Use a <p> rather than <Label> here — the section
                        heading is not the accessible name of any specific
                        form control; the button below has its own visible
                        text. <Label> without htmlFor renders an unbound
                        <label> element which screen readers report as
                        invalid. */}
                    <p className="text-sm font-medium leading-none">
                      {t("refreshFromIdP", { provider: refreshProvider })}
                    </p>
                    <p className="text-xs text-slate-500">
                      {t("refreshFromIdPDesc", { provider: refreshProvider })}
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleRefreshFromIdP}
                      disabled={isRefreshing}
                    >
                      <RefreshCw
                        className={`h-4 w-4 mr-2 ${isRefreshing ? "animate-spin" : ""}`}
                      />
                      {isRefreshing
                        ? t("refreshFromIdPLoading")
                        : t("refreshFromIdPButton", {
                            provider: refreshProvider,
                          })}
                    </Button>
                  </div>
                );
              })()}

              <div className="space-y-2">
                <Label htmlFor="timezone">{t("timezone")}</Label>
                <Select
                  value={timezone}
                  onValueChange={setTimezone}
                  disabled={!isEditMode}
                >
                  <SelectTrigger id="timezone">
                    <SelectValue placeholder={t("selectTimezone")} />
                  </SelectTrigger>
                  <SelectContent>
                    {COMMON_TIMEZONES.map((tz) => (
                      <SelectItem key={tz.value} value={tz.value}>
                        {tz.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-slate-500">{t("timezoneDesc")}</p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="locale">{t("locale")}</Label>
                <Select
                  value={locale}
                  onValueChange={setLocale}
                  disabled={!isEditMode}
                >
                  <SelectTrigger id="locale">
                    <SelectValue placeholder={t("selectLocale")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ja">{t("localeJa")}</SelectItem>
                    <SelectItem value="en">{t("localeEn")}</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-slate-500">{t("localeDesc")}</p>
              </div>

              {user.role === "admin" && (
                <div className="space-y-2">
                  <Label htmlFor="role">{t("systemRole")}</Label>
                  <Input
                    id="role"
                    value={t("systemAdmin")}
                    disabled
                    className="bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 font-medium border-red-200 dark:border-red-800"
                  />
                  <p className="text-xs text-red-600 dark:text-red-400">
                    🛡️ {t("elevatedPrivileges")}
                  </p>
                </div>
              )}

              {isEditMode && (
                <div className="flex gap-2 pt-2">
                  <Button onClick={handleSaveProfile}>
                    <Save className="h-4 w-4 mr-2" />
                    {tCommon("saveChanges")}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setIsEditMode(false);
                      setName(user.name || "");
                      setTimezone(user.timezone || "UTC");
                      setLocale(user.locale || "en");
                    }}
                  >
                    {tCommon("cancel")}
                  </Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Connected accounts (Issue #517): link/unlink Google & GitHub.
          The read-only "Sign-in method" field above (#514) and the
          refresh-from-IdP control (#515) are intentionally retained — they are
          covered by an extensive test suite and serve a distinct purpose
          (current method display + IdP profile refresh) from this management
          section, so removing them was judged riskier than additive mounting. */}
      <ConnectedAccounts />

      {/* Theme & Appearance */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {isDarkMode ? (
              <Moon className="h-5 w-5" />
            ) : (
              <Sun className="h-5 w-5" />
            )}
            {t("themeAppearance")}
          </CardTitle>
          <CardDescription>{t("themeAppearanceDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label htmlFor="dark-mode">{t("darkMode")}</Label>
              <p className="text-sm text-slate-500">{t("darkModeDesc")}</p>
            </div>
            <Switch
              id="dark-mode"
              checked={isDarkMode}
              onCheckedChange={handleToggleDarkMode}
            />
          </div>
        </CardContent>
      </Card>

      {/* Danger zone: self-serve account & data deletion (Issue #953) */}
      <DeleteAccountSection />
    </PageContainer>
  );
}
