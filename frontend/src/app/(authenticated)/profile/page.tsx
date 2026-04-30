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
import { useRouter, useSearchParams } from "next/navigation";
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
import { useToast } from "@/hooks/use-toast";
import { User, Moon, Sun, Save, RefreshCw } from "lucide-react";
import { COMMON_TIMEZONES } from "@/lib/utils/datetime";
import { apiClient, ApiError } from "@/lib/api/base";
import type { User as AuthUser } from "@/lib/auth/auth";

/**
 * Issue #514: derive the i18n label for the user's sign-in method.
 * Password users always show "Email + Password" regardless of auth_provider.
 * OAuth users with a known provider show that provider's name.
 * Pre-#361 OAuth users may have auth_provider=null; fall back to "Other".
 */
export function getSignInMethodLabel(
  user: Pick<AuthUser, "auth_method" | "auth_provider">,
  t: (key: string) => string,
): string {
  if (user.auth_method === "password") return t("signInMethodPassword");
  if (user.auth_provider === "google") return t("signInMethodGoogle");
  if (user.auth_provider === "github") return t("signInMethodGitHub");
  return t("signInMethodOther");
}

/**
 * Issue #515: human-readable provider name for i18n message interpolation.
 * Returns null when refresh is not available for the user (password auth or
 * legacy OAuth row with no recorded provider).
 */
export function getRefreshProviderName(
  user: Pick<AuthUser, "auth_method" | "auth_provider">,
): "Google" | "GitHub" | null {
  if (user.auth_method !== "oauth") return null;
  if (user.auth_provider === "google") return "Google";
  if (user.auth_provider === "github") return "GitHub";
  return null;
}

export default function ProfilePage() {
  const t = useTranslations("profile");
  const tCommon = useTranslations("common");
  const { user, refetchUser } = useAuth();
  const { toast } = useToast();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isEditMode, setIsEditMode] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Issue #515: handle the post-IdP-redirect search params
  // (refreshed=1 / error=refresh_*). Surface as toasts then strip the
  // params so a browser refresh of the page doesn't replay the toast.
  // useSearchParams reference is stable; keys are read inside the effect
  // so we don't need to depend on the SearchParams object identity.
  // useAuth().refetchUser is included so a successful refresh re-pulls
  // the auth context and the new email/name appear immediately.
  useEffect(() => {
    const refreshed = searchParams.get("refreshed");
    const errorCode = searchParams.get("error");
    const provider = getRefreshProviderName(user ?? {}) ?? "IdP";
    const cleanUrl = "/profile";

    if (refreshed === "1") {
      toast({
        title: t("refreshFromIdPSuccess", { provider }),
        description: t("refreshFromIdPSuccessDesc"),
      });
      // Re-pull /auth/me so the new email/name reflect in the form.
      refetchUser();
      router.replace(cleanUrl);
    } else if (errorCode?.startsWith("refresh_")) {
      const messageKey =
        errorCode === "refresh_user_mismatch"
          ? "refreshFromIdPErrorMismatch"
          : errorCode === "refresh_state_expired"
            ? "refreshFromIdPErrorExpired"
            : "refreshFromIdPErrorGeneric";
      toast({
        title: tCommon("error"),
        description: t(messageKey, { provider }),
        variant: "destructive",
      });
      router.replace(cleanUrl);
    }
    // We intentionally only react to the params on first paint after
    // the IdP redirect. Re-running on every render would cause loops.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRefreshFromIdP = async () => {
    const provider = getRefreshProviderName(user ?? {});
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

  // Profile form state
  const [name, setName] = useState(user?.name || "");
  const [email] = useState(user?.email || ""); // Email is read-only
  const [timezone, setTimezone] = useState(user?.timezone || "UTC");

  // Sync state with user data when it changes
  useEffect(() => {
    if (user) {
      setName(user.name || "");
      setTimezone(user.timezone || "UTC");
    }
  }, [user]);

  const handleSaveProfile = async () => {
    try {
      await apiClient.put("/api/v1/users/profile", {
        name,
        timezone,
      });

      // Refresh user data to get updated timezone
      await refetchUser();

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
    <div className="space-y-6 max-w-4xl">
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
                const refreshProvider = getRefreshProviderName(user);
                if (!refreshProvider) return null;
                return (
                  <div className="space-y-2 rounded-md border border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/30 p-3">
                    <Label className="text-sm">
                      {t("refreshFromIdP", { provider: refreshProvider })}
                    </Label>
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
    </div>
  );
}
