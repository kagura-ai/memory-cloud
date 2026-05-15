import Link from "next/link";
import { useTranslations } from "next-intl";

// Explicit not-found page. Replaces Next.js's generic fallback with a
// branded 404 that follows the dashboard's Tailwind styling and renders
// localized copy through next-intl (en/ja). Authored during the #643
// investigation; #643's actual root cause was a Docker `--env-file`
// omission, not the default `/_not-found` route — this page is shipped
// independently as a UX improvement, not a build-failure workaround.
export const metadata = {
  title: "404 — Page Not Found",
};

export default function NotFound() {
  const t = useTranslations("notFoundPage");
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-6 py-12 text-center">
      <h1 className="mb-4 text-6xl font-bold tracking-tight">{t("title")}</h1>
      <p className="mb-8 text-lg text-gray-600 dark:text-gray-400">
        {t("description")}
      </p>
      <Link
        href="/"
        className="rounded-md bg-blue-600 px-6 py-2 text-white transition hover:bg-blue-700"
      >
        {t("returnHome")}
      </Link>
    </div>
  );
}
