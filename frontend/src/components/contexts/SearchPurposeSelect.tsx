"use client";

/**
 * SearchPurposeSelect (#2983) — the Purpose select of a context's search
 * settings and of the advanced create dialog, with a line per purpose on
 * which content it suits. The caller owns the value and what a choice does.
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
  type SearchPurpose,
} from "@/lib/searchPurposes";

interface SearchPurposeSelectProps<P extends SearchPurpose> {
  id: string;
  value: P;
  purposes: readonly P[];
  onChange: (purpose: P) => void;
  disabled?: boolean;
  /** A line under the guidance (e.g. that the values can be changed later). */
  hint?: string;
}

const CONVERSATION = SEARCH_PURPOSE_PRESETS.conversation;
const DOCUMENTS = SEARCH_PURPOSE_PRESETS.documents;

export function SearchPurposeSelect<P extends SearchPurpose>({
  id,
  value,
  purposes,
  onChange,
  disabled,
  hint,
}: SearchPurposeSelectProps<P>) {
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

  return (
    <div className="space-y-3">
      <Label htmlFor={id}>{t("purpose.label")}</Label>
      <Select
        value={value}
        onValueChange={(next) => onChange(next as P)}
        disabled={disabled}
      >
        <SelectTrigger id={id}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {purposes.map((purpose) => (
            <SelectItem key={purpose} value={purpose}>
              {label(purpose)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <ul className="space-y-1 text-sm text-muted-foreground">
        {purposes.map((purpose) => (
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
