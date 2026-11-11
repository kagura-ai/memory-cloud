"use client";

import { ArrowUpRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useSystemInfo } from "@/hooks/useSystemFeatures";

/**
 * The deployment's ChatGPT app listing (#2760), or null when there is nothing
 * safe to link to. The URL is a deployment setting (`CHATGPT_APP_URL`, never
 * hardcoded: a listing connects to one MCP server and this app is also run
 * self-hosted). The backend refuses a non-https value at startup; the UI still
 * never turns an arbitrary string into a link, whatever the API returned.
 */
export function chatGptAppHref(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    // No userinfo: `https://a.example@b.example` reads as one host, opens another.
    const safe = url.protocol === "https:" && url.hostname && !url.username && !url.password;
    return safe ? value : null;
  } catch {
    return null;
  }
}

/**
 * Opens the listing in a new tab, next to the plugin guide link (#2736).
 * Renders nothing — no placeholder — while system info loads or when the
 * deployment sets no URL, so an unset deployment looks exactly as before.
 */
export function ChatGptAppLink() {
  const info = useSystemInfo();
  const t = useTranslations("chatgptApp");
  const href = chatGptAppHref(info?.chatgpt_app_url);
  if (!href) return null;
  return (
    <div className="space-y-1">
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-primary underline underline-offset-4 hover:text-primary/80"
      >
        {t("label")}
        <ArrowUpRight aria-hidden="true" className="h-4 w-4" />
        <span className="sr-only">{t("newTab")}</span>
      </a>
      <p className="text-xs leading-5 text-muted-foreground">{t("description")}</p>
    </div>
  );
}
