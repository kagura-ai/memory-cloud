"use client";

/**
 * #1758 / #1880: the create dialog's "reconnect Slack" notice.
 *
 * The pending install reports the feature scopes its grant lacks. Ingestion
 * works without them, so this warns rather than blocks — and it lists only
 * the features that are actually affected, one message per scope, instead of
 * explaining all three whatever is missing.
 */

import { useTranslations } from "next-intl";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

// Slack scope -> message key. Scope names carry ":" and "_", so they are not
// used as message keys themselves.
const FEATURE_KEYS = {
  commands: "missingScopeFeatures.commands",
  "app_mentions:read": "missingScopeFeatures.appMentionsRead",
  "groups:read": "missingScopeFeatures.groupsRead",
} as const;

function isKnownScope(scope: string): scope is keyof typeof FEATURE_KEYS {
  return Object.hasOwn(FEATURE_KEYS, scope);
}

export function MissingScopesNotice({ scopes }: { scopes: string[] }) {
  const t = useTranslations("connectors");
  if (scopes.length === 0) return null;
  return (
    <Alert variant="warning">
      <AlertTitle>{t("missingScopesTitle")}</AlertTitle>
      <AlertDescription className="space-y-2">
        <p>{t("missingScopesDesc")}</p>
        <ul className="list-disc space-y-1 pl-5">
          {scopes.map((scope) => (
            <li key={scope}>
              {isKnownScope(scope)
                ? t(FEATURE_KEYS[scope])
                : // A scope a newer server reports: name it rather than hide it.
                  t("missingScopeFeatures.other", { scope })}
            </li>
          ))}
        </ul>
        <p>{t("missingScopesAction", { count: scopes.length })}</p>
      </AlertDescription>
    </Alert>
  );
}
