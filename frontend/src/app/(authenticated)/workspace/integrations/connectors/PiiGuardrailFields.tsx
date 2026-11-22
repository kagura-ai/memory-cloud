"use client";

/**
 * #890 / #2971: the PII masking controls, shared by the create dialog and the
 * connector settings dialog so both show the same choices and build the same
 * `pii_guardrail_config`.
 *
 * The form state is a draft (`PiiDraft`); `piiConfigFromDraft` turns it into
 * the request body. The backend validates the full shape (unknown keys
 * refused, a redaction enum, a non-blank locale, detectors required while
 * enabled).
 */

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { PiiGuardrailConfig } from "@/lib/api/workspace-connectors";

// Presidio recognizer names offered in the UI. Kept in sync with the worker's
// recognizer set.
export const PII_DETECTORS = [
  "EMAIL_ADDRESS",
  "PHONE_NUMBER",
  "CREDIT_CARD",
  "PERSON",
  "IP_ADDRESS",
  "IBAN_CODE",
] as const;
export const PII_DEFAULT_DETECTORS = [
  "EMAIL_ADDRESS",
  "PHONE_NUMBER",
  "CREDIT_CARD",
  "PERSON",
];
const PII_REDACTION_MODES = ["mask", "hash", "remove"] as const;
type PiiRedaction = (typeof PII_REDACTION_MODES)[number];

export interface PiiDraft {
  enabled: boolean;
  detectors: string[];
  redaction: PiiRedaction;
  failClosed: boolean;
  // Not edited in the UI: the create form derives it from the UI locale, the
  // settings form keeps the stored one.
  locale: string;
}

/** The defaults a new connector gets: masking on, so an untouched form is safe. */
export function piiDefaultDraft(locale: string): PiiDraft {
  return {
    enabled: true,
    detectors: [...PII_DEFAULT_DETECTORS],
    redaction: "mask",
    failClosed: true,
    locale,
  };
}

/**
 * The draft for a stored config. Nothing stored (a connector created before
 * PII masking existed) gives the create form's defaults.
 *
 * The column is untyped JSONB and a config written before validation (#866)
 * may lack a field or carry a wrong type, so every field is read on its own and
 * falls back to its default; this never throws.
 */
export function piiDraftFromStored(
  stored: PiiGuardrailConfig | null | undefined,
  uiLocale: string,
): PiiDraft {
  const defaults = piiDefaultDraft(uiLocale);
  if (!stored || typeof stored !== "object") return defaults;
  const raw = stored as Partial<Record<keyof PiiGuardrailConfig, unknown>>;
  const detectors = Array.isArray(raw.detectors)
    ? raw.detectors.filter(
        (d): d is string => typeof d === "string" && d.trim() !== "",
      )
    : [];
  return {
    enabled: typeof raw.enabled === "boolean" ? raw.enabled : defaults.enabled,
    // A switched-off config is stored with no detectors; turning it back on
    // starts from the defaults rather than from an empty, unsavable list.
    detectors: detectors.length > 0 ? detectors : defaults.detectors,
    redaction: PII_REDACTION_MODES.find((m) => m === raw.redaction) ?? "mask",
    failClosed:
      typeof raw.fail_closed === "boolean"
        ? raw.fail_closed
        : defaults.failClosed,
    locale:
      typeof raw.locale === "string" && raw.locale.trim()
        ? raw.locale
        : uiLocale,
  };
}

/** The request body for a draft. A switched-off draft sends no detectors. */
export function piiConfigFromDraft(draft: PiiDraft): PiiGuardrailConfig {
  return {
    enabled: draft.enabled,
    detectors: draft.enabled ? draft.detectors : [],
    redaction: draft.redaction,
    locale: draft.locale,
    fail_closed: draft.failClosed,
  };
}

/** Whether two drafts would store the same config (detector order ignored). */
export function piiDraftsEqual(a: PiiDraft, b: PiiDraft): boolean {
  const key = (d: PiiDraft) => {
    const config = piiConfigFromDraft(d);
    return JSON.stringify({
      ...config,
      detectors: [...config.detectors].sort(),
    });
  };
  return key(a) === key(b);
}

/** An enabled draft needs at least one detector (the backend refuses it). */
export function piiDraftInvalid(draft: PiiDraft): boolean {
  return draft.enabled && draft.detectors.length === 0;
}

export function PiiGuardrailFields({
  value,
  onChange,
  idPrefix,
  disabled = false,
  notice,
  storedDetectors = [],
}: {
  value: PiiDraft;
  onChange: (next: PiiDraft) => void;
  // Keeps the redaction select's id unique per dialog.
  idPrefix: string;
  disabled?: boolean;
  // Stored detectors the form does not offer: shown as extra choices (and
  // kept after being unchecked) so no selection is ever hidden.
  storedDetectors?: readonly string[];
  // Extra line under the description (the settings dialog's "nothing stored
  // yet" note).
  notice?: ReactNode;
}) {
  const t = useTranslations("connectors");
  const redactionId = `${idPrefix}-pii-redaction`;
  const offered: readonly string[] = PII_DETECTORS;
  const choices = [
    ...offered,
    ...new Set(
      [...storedDetectors, ...value.detectors].filter(
        (d) => !offered.includes(d),
      ),
    ),
  ];
  return (
    <div>
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm font-medium">{t("piiTitle")}</p>
          <p className="text-xs text-muted-foreground">{t("piiDesc")}</p>
          {notice}
        </div>
        <label className="flex shrink-0 items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={value.enabled}
            disabled={disabled}
            onChange={(e) => onChange({ ...value, enabled: e.target.checked })}
          />
          {t("piiEnabled")}
        </label>
      </div>

      {value.enabled && (
        <div className="mt-3 space-y-3">
          <div>
            <p className="mb-1 text-xs font-medium">{t("piiDetectors")}</p>
            <div className="grid grid-cols-2 gap-1">
              {choices.map((d) => (
                <label key={d} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={value.detectors.includes(d)}
                    disabled={disabled}
                    onChange={(e) =>
                      onChange({
                        ...value,
                        detectors: e.target.checked
                          ? [...value.detectors, d]
                          : value.detectors.filter((x) => x !== d),
                      })
                    }
                  />
                  <span className="font-mono text-xs">{d}</span>
                </label>
              ))}
            </div>
            {value.detectors.length === 0 && (
              <p className="mt-1 text-xs text-destructive">
                {t("piiDetectorsRequired")}
              </p>
            )}
          </div>
          <div>
            <label
              htmlFor={redactionId}
              className="mb-1 block text-xs font-medium"
            >
              {t("piiRedaction")}
            </label>
            <Select
              value={value.redaction}
              disabled={disabled}
              onValueChange={(v) =>
                onChange({ ...value, redaction: v as PiiRedaction })
              }
            >
              <SelectTrigger id={redactionId} className="h-9">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PII_REDACTION_MODES.map((m) => (
                  <SelectItem key={m} value={m}>
                    {t(`piiRedaction_${m}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={value.failClosed}
              disabled={disabled}
              onChange={(e) =>
                onChange({ ...value, failClosed: e.target.checked })
              }
            />
            {t("piiFailClosed")}
          </label>
        </div>
      )}
    </div>
  );
}
