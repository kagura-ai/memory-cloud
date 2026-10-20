/**
 * DefaultContextControl (#2129)
 *
 * Inline control for a credential's MCP default context — an API key or an
 * authorized OAuth app. A context-scoped MCP tool called with that credential
 * and without `context_id` runs in the default (`?context=` on the URL wins).
 *
 * A native select (like the create dialog's context picker) plus Save, and a
 * Clear button while a default is set. The caller performs the request and
 * throws on failure; the control only tracks the pending choice.
 */

"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { InlineSpinner } from "@/components/common/LoadingState";

export interface DefaultContextOption {
  id: string;
  label: string;
}

interface DefaultContextControlProps {
  /** The credential's current default, `null` for none. */
  currentId: string | null;
  /** Its readable name; `null` when unset or no longer readable. */
  currentName: string | null;
  /** The contexts offered (the current workspace's). */
  options: DefaultContextOption[];
  /** Persist the choice (`null` clears). Throw to keep the pending choice. */
  onSave: (contextId: string | null) => Promise<void>;
  /** Accessible name of the select, e.g. "Default context for <key>". */
  ariaLabel: string;
}

/** Read-only rendering: the default's name, "—" for none. */
export function DefaultContextName({
  currentId,
  currentName,
}: {
  currentId: string | null;
  currentName: string | null;
}) {
  const t = useTranslations("defaultContext");
  if (!currentId) {
    return (
      <span className="text-gray-400" aria-label={t("none")}>
        —
      </span>
    );
  }
  return (
    <span className={currentName ? undefined : "text-amber-700 dark:text-amber-300"}>
      {currentName ?? t("unavailable")}
    </span>
  );
}

export function DefaultContextControl({
  currentId,
  currentName,
  options,
  onSave,
  ariaLabel,
}: DefaultContextControlProps) {
  const t = useTranslations("defaultContext");
  const [selected, setSelected] = useState(currentId ?? "");
  const [saving, setSaving] = useState(false);

  // A saved (or externally reloaded) value becomes the new baseline.
  useEffect(() => {
    setSelected(currentId ?? "");
  }, [currentId]);

  // The current default may live outside the offered list (another
  // workspace, or a context the owner can no longer read): keep it
  // selectable so the select shows the truth instead of "No default".
  const shownOptions =
    currentId && !options.some((o) => o.id === currentId)
      ? [{ id: currentId, label: currentName ?? t("unavailable") }, ...options]
      : options;

  const save = async (contextId: string | null) => {
    setSaving(true);
    try {
      await onSave(contextId);
    } catch {
      // The caller reported the failure; keep the pending choice.
    } finally {
      setSaving(false);
    }
  };

  const changed = selected !== (currentId ?? "");

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        aria-label={ariaLabel}
        className="max-w-[14rem] rounded-md border border-input bg-background px-2 py-1 text-sm"
        value={selected}
        disabled={saving}
        onChange={(e) => setSelected(e.target.value)}
      >
        <option value="">{t("noDefault")}</option>
        {shownOptions.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
      </select>
      {changed && (
        <Button
          type="button"
          size="sm"
          disabled={saving}
          onClick={() => void save(selected || null)}
        >
          {saving ? <InlineSpinner size="sm" /> : t("save")}
        </Button>
      )}
      {!changed && currentId && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={saving}
          onClick={() => void save(null)}
        >
          {saving ? <InlineSpinner size="sm" /> : t("clear")}
        </Button>
      )}
    </div>
  );
}
