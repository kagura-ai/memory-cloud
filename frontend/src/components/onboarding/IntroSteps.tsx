"use client";

/**
 * The welcome dialog's first two screens (#2701), shown before the optional
 * in-app practice in OnboardingCard: the user's language and timezone, then
 * connecting an AI chat client over MCP — how the product is actually used.
 */

import Link from "next/link";
import { useId, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowRight, Check, Copy } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import {
  defaultLocale,
  localeNames,
  locales,
  useLocale,
  type Locale,
} from "@/i18n";
import { API_BASE_URL, updateUserProfile } from "@/lib/api/base";
import { mcpEndpoints } from "@/lib/mcp/url";
import { timezoneOptions } from "@/lib/utils/datetime";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { InlineSpinner } from "@/components/common/LoadingState";

/** The connection card on the Integrations entry, with the per-client steps. */
export const MCP_CONNECTION_HREF = "/workspace/integrations#mcp-connection";

/**
 * The welcome dialog's primary actions in the brand green (#047857: white
 * text at 5.5:1, WCAG AA), set apart from the black buttons of the app.
 */
export const PRIMARY_BUTTON =
  "bg-brand-green-600 text-white hover:bg-brand-green-700";

const COPY_KEY = "onboarding-mcp-url";
/** The timezone a new account starts with (backend column default). */
const DEFAULT_TIMEZONE = "UTC";

/** The first of the browser's preferred languages the app offers, else null. */
export function browserLocale(): Locale | null {
  try {
    const preferred = navigator.languages?.length
      ? navigator.languages
      : [navigator.language];
    for (const tag of preferred) {
      const match = locales.find(
        (locale) => locale === tag?.toLowerCase().split("-")[0],
      );
      if (match) return match;
    }
  } catch {
    // No navigator (SSR): nothing detected.
  }
  return null;
}

/** The browser's IANA timezone, else null. */
export function browserTimezone(): string | null {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone || zone === "Etc/Unknown") return null;
    // Only a name the platform lists: the server validates with tzdata and
    // refuses unknown names. UTC is not in every platform's list.
    const known =
      typeof Intl.supportedValuesOf === "function"
        ? Intl.supportedValuesOf("timeZone")
        : null;
    return zone === "UTC" || !known || known.includes(zone) ? zone : null;
  } catch {
    return null;
  }
}

/**
 * The values the screen starts with. A value the user already chose (anything
 * but the account defaults) wins over the browser, so saving never overwrites
 * it; the browser fills in only what was never set.
 */
function initialPreferences(
  saved: { locale?: string | null; timezone?: string | null } | null,
  uiLocale: Locale,
) {
  const browserLanguage = browserLocale();
  const browserZone = browserTimezone();
  const savedLanguage = locales.find((locale) => locale === saved?.locale);
  const savedZone = saved?.timezone || null;
  const keepLanguage =
    savedLanguage !== undefined && savedLanguage !== defaultLocale;
  const keepZone = savedZone !== null && savedZone !== DEFAULT_TIMEZONE;
  return {
    language: keepLanguage
      ? savedLanguage
      : (browserLanguage ?? savedLanguage ?? uiLocale),
    timezone: keepZone
      ? savedZone
      : (browserZone ?? savedZone ?? DEFAULT_TIMEZONE),
    detected:
      (!keepLanguage && browserLanguage !== null) ||
      (!keepZone && browserZone !== null),
  };
}

export function PreferencesStep({
  onDone,
  onBusyChange,
}: {
  onDone: () => void;
  /** True while the save is in flight, so the dialog cannot close under it. */
  onBusyChange?: (busy: boolean) => void;
}) {
  const t = useTranslations("onboarding");
  const { user, refetchUser } = useAuth();
  const { locale: uiLocale, setLocale } = useLocale();
  const languageId = useId();
  const timezoneId = useId();
  const [initial] = useState(() => initialPreferences(user, uiLocale));
  const [language, setLanguage] = useState<Locale>(initial.language);
  const [timezone, setTimezone] = useState(initial.timezone);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // The common list, plus the starting zone when it is not on it (a saved or
  // detected zone such as Europe/Berlin), so it is never swapped for UTC.
  const [zones] = useState(() => timezoneOptions(initial.timezone));

  const save = async () => {
    setBusy(true);
    setFailed(false);
    onBusyChange?.(true);
    try {
      await updateUserProfile({ locale: language, timezone });
      // Refresh before applying: the layout applies the signed-in user's saved
      // locale whenever it differs from the UI, so switching over a stale
      // record would be switched straight back. refetchUser resolves
      // undefined on a transient failure instead of throwing.
      const fresh = await refetchUser();
      if (!fresh) throw new Error("The signed-in user was not refreshed");
      setLocale(language);
      onDone();
    } catch {
      setFailed(true);
      setBusy(false);
    } finally {
      onBusyChange?.(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="font-semibold">{t("prefs.title")}</h3>
        <p className="text-sm text-muted-foreground">{t("prefs.body")}</p>
      </div>
      {failed && (
        <Alert variant="destructive">
          <AlertDescription>{t("prefs.saveFailed")}</AlertDescription>
        </Alert>
      )}
      <div className="space-y-2">
        <Label htmlFor={languageId}>{t("prefs.language")}</Label>
        <Select
          value={language}
          onValueChange={(value) => setLanguage(value as Locale)}
        >
          <SelectTrigger id={languageId} className="w-full sm:max-w-sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {locales.map((locale) => (
              <SelectItem key={locale} value={locale}>
                {localeNames[locale]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-2">
        <Label htmlFor={timezoneId}>{t("prefs.timezone")}</Label>
        <Select value={timezone} onValueChange={setTimezone}>
          <SelectTrigger id={timezoneId} className="w-full sm:max-w-sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {zones.map((tz) => (
              <SelectItem key={tz.value} value={tz.value}>
                {tz.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {initial.detected && (
        <p className="text-xs text-muted-foreground">{t("prefs.detected")}</p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button onClick={save} disabled={busy} className={PRIMARY_BUTTON}>
          {busy && <InlineSpinner />}
          {t("prefs.save")}
          <ArrowRight className="ml-2 h-4 w-4" />
        </Button>
        <Button variant="outline" onClick={onDone} disabled={busy}>
          {t("prefs.skip")}
        </Button>
      </div>
    </div>
  );
}

export function ChatStep({
  onNext,
  onGuide,
}: {
  onNext: () => void;
  /** The user leaves for the per-client steps: count the screen as seen. */
  onGuide: () => void;
}) {
  const t = useTranslations("onboarding");
  const common = useTranslations("common");
  const { isCopied, copyToTarget } = useCopyFeedback();
  const [copyFailed, setCopyFailed] = useState(false);
  // The bare URL follows the workspace selected in the web UI, which is all a
  // new owner has; pinning lives on the Integrations entry.
  const { mcpUrl } = mcpEndpoints(API_BASE_URL, null);

  const copy = async () => {
    setCopyFailed(false);
    try {
      await copyToTarget(mcpUrl, COPY_KEY);
    } catch {
      // Inside a dialog the failure stays in the dialog; the URL is
      // select-all, so the hint to copy by hand is enough.
      setCopyFailed(true);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="font-semibold">{t("chat.title")}</h3>
        <p className="text-sm text-muted-foreground">{t("chat.body")}</p>
      </div>
      {copyFailed && (
        <Alert variant="destructive">
          <AlertDescription>{common("copyFailedManualHint")}</AlertDescription>
        </Alert>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
        <code className="min-w-0 flex-1 select-all break-all rounded-md border bg-muted/30 px-3 py-2 font-mono text-sm">
          {mcpUrl}
        </code>
        <Button
          type="button"
          variant="outline"
          onClick={copy}
          className="shrink-0 gap-2"
        >
          {isCopied(COPY_KEY) ? (
            <Check aria-hidden="true" className="h-4 w-4" />
          ) : (
            <Copy aria-hidden="true" className="h-4 w-4" />
          )}
          <span aria-live="polite">
            {isCopied(COPY_KEY) ? t("chat.copied") : t("chat.copy")}
          </span>
        </Button>
      </div>
      <Link
        href={MCP_CONNECTION_HREF}
        onClick={(event) => {
          // Only a same-tab visit leaves this screen; a link opened in a new
          // tab keeps the URL here to copy.
          if (
            event.button === 0 &&
            !event.metaKey &&
            !event.ctrlKey &&
            !event.shiftKey &&
            !event.altKey
          ) {
            onGuide();
          }
        }}
        className="inline-block text-sm text-primary underline"
      >
        {t("chat.guide")}
      </Link>
      <div>
        <Button onClick={onNext} className={PRIMARY_BUTTON}>
          {t("chat.next")}
          <ArrowRight className="ml-2 h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
