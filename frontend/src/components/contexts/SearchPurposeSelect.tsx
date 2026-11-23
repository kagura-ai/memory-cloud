"use client";

/**
 * SearchPurposeSelect (#2983) — the Purpose select of a context's search
 * settings and of the advanced create dialog, with a line per purpose on
 * which content it suits. The caller owns the value and what a choice does.
 *
 * Only the purposes that carry values can be chosen. Custom is a state, not
 * a choice: with `withCustom` it is listed as a disabled item, so the trigger
 * names it while the current values match no purpose, and its guidance says
 * to set the values below.
 */

import { useTranslations } from "next-intl";

import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  SEARCH_PURPOSE_PRESETS,
  type PresetSearchPurpose,
  type SearchPurpose,
} from "@/lib/searchPurposes";

interface SearchPurposeSelectProps {
  id: string;
  value: SearchPurpose;
  /** The purposes that can be chosen. */
  purposes: readonly PresetSearchPurpose[];
  onChange: (purpose: PresetSearchPurpose) => void;
  /** List Custom (not selectable) for values that match no purpose. */
  withCustom?: boolean;
  disabled?: boolean;
  /** A line under the guidance (e.g. that the values can be changed later). */
  hint?: string;
}

const CONVERSATION = SEARCH_PURPOSE_PRESETS.conversation;
const DOCUMENTS = SEARCH_PURPOSE_PRESETS.documents;

export function SearchPurposeSelect({
  id,
  value,
  purposes,
  onChange,
  withCustom,
  disabled,
  hint,
}: SearchPurposeSelectProps) {
  const t = useTranslations("searchSettings");

  // Literal keys on purpose: the message-orphan scan (parity.test.ts) reads
  // a computed key as a reference to the whole namespace.
  const label = (purpose: SearchPurpose) => {
    switch (purpose) {
      case "conversation":
        return t("purpose.conversation");
      case "documents":
        return t("purpose.documents");
      case "custom":
        return t("purpose.custom");
    }
  };
  const guidance = (purpose: SearchPurpose) => {
    switch (purpose) {
      case "conversation":
        return t("purpose.conversationGuidance", {
          semantic: CONVERSATION.semantic_weight.toFixed(1),
          keyword: CONVERSATION.bm25_weight.toFixed(1),
        });
      case "documents":
        return t("purpose.documentsGuidance", {
          semantic: DOCUMENTS.semantic_weight.toFixed(1),
          keyword: DOCUMENTS.bm25_weight.toFixed(1),
        });
      case "custom":
        return t("purpose.customGuidance");
    }
  };

  const listed: readonly SearchPurpose[] = withCustom
    ? [...purposes, "custom"]
    : purposes;
  // A value outside the choosable purposes (Radix can report "" on a form
  // reset) is not a choice.
  const handleValueChange = (next: string) => {
    const chosen = purposes.find((purpose) => purpose === next);
    if (chosen) onChange(chosen);
  };

  return (
    <div className="space-y-3">
      <Label htmlFor={id}>{t("purpose.label")}</Label>
      <Select
        value={value}
        onValueChange={handleValueChange}
        disabled={disabled}
      >
        <SelectTrigger id={id}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {listed.map((purpose) => (
            <SelectItem
              key={purpose}
              value={purpose}
              disabled={purpose === "custom"}
            >
              {label(purpose)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <ul className="space-y-1 text-sm text-muted-foreground">
        {listed.map((purpose) => (
          <li key={purpose}>
            <span className="font-medium text-foreground">
              {label(purpose)}
            </span>
            {": "}
            <span>{guidance(purpose)}</span>
          </li>
        ))}
      </ul>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}
