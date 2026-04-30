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
import { useToast } from "@/hooks/use-toast";
import { User, Moon, Sun, Save } from "lucide-react";
import { COMMON_TIMEZONES } from "@/lib/utils/datetime";
import { apiClient } from "@/lib/api/base";
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

export default function ProfilePage() {
  const t = useTranslations("profile");
  const tCommon = useTranslations("common");
  const { user, refetchUser } = useAuth();
  const { toast } = useToast();
  const [isEditMode, setIsEditMode] = useState(false);
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
