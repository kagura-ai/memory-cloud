"use client";

/**
 * The announcement-email opt-in checkbox on the sign-up screens (#2761):
 * /login (Google / GitHub sign-up), /join/[token], /invite/[token] and the
 * email sign-up form.
 *
 * Always separate from the terms checkbox (`TermsAgreement`), unchecked by
 * default and optional: it never disables a sign-up button. The pages render it
 * only while `/system/info` names an `announcement_consent_version`, and send
 * the answer (ticked or not) with that version; the backend records it for a
 * NEW account only.
 *
 * `/login` is always light (#1029); `/join` and `/invite` follow the theme, so
 * they pass `themed` to add the dark-mode text colour.
 */

import { useTranslations } from "next-intl";

export function AnnouncementEmailsOptIn({
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
  const t = useTranslations("announcementEmails");
  const textClass = themed
    ? "text-sm text-gray-700 dark:text-gray-300"
    : "text-sm text-gray-700";

  return (
    <label className="flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onCheckedChange(e.target.checked)}
        data-testid={testId}
        className="mt-1 h-4 w-4 rounded border-gray-300 text-kagura-accent focus:ring-kagura-accent"
      />
      <span className={textClass}>{t("optIn")}</span>
    </label>
  );
}
