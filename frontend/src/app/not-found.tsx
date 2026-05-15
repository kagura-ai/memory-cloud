"use client";

import Link from "next/link";
import { useLocale } from "@/i18n";

// Explicit not-found page. Replaces Next.js's generic fallback with a
// branded 404 that follows the dashboard's Tailwind styling and renders
// localized copy via the project's `useLocale()` hook (en/ja). Authored
// during the #643 investigation; #643's actual root cause was a Docker
// `--env-file` omission, not the default `/_not-found` route — this
// page is shipped independently as a UX improvement, not a build-
// failure workaround.
//
// Localized strings are inlined here (not pulled through
// `useTranslations` / `messages/*.json`) because next-intl's
// `useTranslations` hook destabilizes the Turbopack build worker on
// the static prerender of this page (consistent SIGSEGV during page
// data collection on Next.js 16.2.6). The project's own `useLocale`
// context only exposes the locale string, so a small inline copy table
// is the smallest robust fix. If `next-intl/request.ts` is later
// configured for server components, this page can be converted back to
// a standard server-rendered i18n flow.

const COPY = {
  en: {
    title: "404",
    description: "The page you are looking for does not exist.",
    returnHome: "Return home",
  },
  ja: {
    title: "404",
    description: "お探しのページは存在しません。",
    returnHome: "ホームに戻る",
  },
} as const;

export default function NotFound() {
  const { locale } = useLocale();
  const t = COPY[locale];
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-6 py-12 text-center">
      <h1 className="mb-4 text-6xl font-bold tracking-tight">{t.title}</h1>
      <p className="mb-8 text-lg text-gray-600 dark:text-gray-400">
        {t.description}
      </p>
      <Link
        href="/"
        className="rounded-md bg-blue-600 px-6 py-2 text-white transition hover:bg-blue-700"
      >
        {t.returnHome}
      </Link>
    </div>
  );
}
