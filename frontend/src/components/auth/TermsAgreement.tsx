"use client";

/**
 * The terms-of-service checkbox shared by /login and /join/[token] (#1655).
 *
 * One component so both entry points ask for the same acceptance with the same
 * copy and links. Acceptance is checked on the client only; recording it on
 * the server is a separate change.
 *
 * `/login` is always light (#1029); `/join` follows the theme, so it passes
 * `themed` to add the dark-mode text colours.
 */

import { useTranslations } from "next-intl";

const TERMS_URL = "https://www.kagura-ai.com/terms";
const PRIVACY_URL = "https://www.kagura-ai.com/privacy";

export function TermsAgreement({
  checked,
  onCheckedChange,
  themed = false,
  testId,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  themed?: boolean;
  testId?: string;
}) {
  const t = useTranslations("login");
  const textClass = themed
    ? "text-sm text-gray-700 dark:text-gray-300"
    : "text-sm text-gray-700";
  const linkClass = themed
    ? "font-medium text-kagura-link hover:underline dark:text-blue-400"
    : "font-medium text-kagura-link hover:underline";

  return (
    <label className="flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onCheckedChange(e.target.checked)}
        data-testid={testId}
        className="mt-1 h-4 w-4 rounded border-gray-300 text-kagura-accent focus:ring-kagura-accent"
      />
      <span className={textClass}>
        {t("agreeToTerms")}{" "}
        <a
          href={TERMS_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={linkClass}
        >
          {t("termsOfService")}
        </a>{" "}
        {t("termsAndPrivacy")}{" "}
        <a
          href={PRIVACY_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={linkClass}
        >
          {t("privacyPolicy")}
        </a>
      </span>
    </label>
  );
}
