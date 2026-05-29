/**
 * TagAutocomplete (#618)
 *
 * Combobox over a comma-separated tag input. As the user types the token after
 * the last comma, it debounces a prefix query to `GET /contexts/{id}/tags` and
 * offers the top matches (excluding tags already present). Selecting one
 * replaces just that trailing token and appends ", " for the next entry.
 *
 * Accessible: role=combobox input + role=listbox/option, ArrowUp/Down to
 * navigate, Enter to select, Esc to close.
 */
"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Input } from "@/components/ui/input";
import { getContextTags, type ContextTagItem } from "@/lib/api/contexts";

const DEBOUNCE_MS = 250;
const SUGGEST_LIMIT = 10;

/** Split a CSV value into the committed prefix and the trailing token-in-progress. */
function activeTokenOf(csv: string): { before: string; token: string } {
  const idx = csv.lastIndexOf(",");
  if (idx === -1) return { before: "", token: csv.trimStart() };
  return {
    before: csv.slice(0, idx + 1),
    token: csv.slice(idx + 1).trimStart(),
  };
}

export interface TagAutocompleteProps {
  contextId: string;
  /** The full CSV string (e.g. "python, auth"). */
  value: string;
  onChange: (csv: string) => void;
  id?: string;
  placeholder?: string;
  ariaLabel?: string;
}

export function TagAutocomplete({
  contextId,
  value,
  onChange,
  id,
  placeholder,
  ariaLabel,
}: TagAutocompleteProps) {
  const t = useTranslations("contextDetail.tagAutocomplete");
  const [open, setOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<ContextTagItem[]>([]);
  const [highlight, setHighlight] = useState(0);
  const reqRef = useRef(0);
  const listId = useId();

  const { before, token } = activeTokenOf(value);

  useEffect(() => {
    const trimmed = token.trim();
    if (trimmed.length < 1) {
      // Invalidate any in-flight request (bump the id) so a prior fetch that
      // resolves after the token was cleared can't re-open the listbox.
      reqRef.current++;
      setSuggestions([]);
      setOpen(false);
      return;
    }
    const timer = window.setTimeout(() => {
      const reqId = ++reqRef.current;
      getContextTags(contextId, {
        prefix: trimmed,
        limit: SUGGEST_LIMIT,
        sort: "count",
      })
        .then((res) => {
          if (reqId !== reqRef.current) return;
          // Don't suggest tags already in the CSV (case-insensitive).
          const present = new Set(
            before
              .split(",")
              .map((s) => s.trim().toLowerCase())
              .filter(Boolean),
          );
          const filtered = res.tags.filter(
            (x) => !present.has(x.tag.toLowerCase()),
          );
          setSuggestions(filtered);
          setHighlight(0);
          setOpen(filtered.length > 0);
        })
        .catch(() => {
          if (reqId === reqRef.current) {
            setSuggestions([]);
            setOpen(false);
          }
        });
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [contextId, token, before]);

  const select = (tag: string) => {
    const prefix = before ? `${before.replace(/\s*$/, "")} ` : "";
    onChange(`${prefix}${tag}, `);
    setOpen(false);
    setSuggestions([]);
  };

  return (
    <div className="relative">
      <Input
        id={id}
        value={value}
        placeholder={placeholder}
        aria-label={ariaLabel}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={
          open && suggestions.length > 0
            ? `${listId}-opt-${highlight}`
            : undefined
        }
        aria-autocomplete="list"
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (!open || suggestions.length === 0) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlight((h) => (h + 1) % suggestions.length);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight(
              (h) => (h - 1 + suggestions.length) % suggestions.length,
            );
          } else if (e.key === "Enter") {
            e.preventDefault();
            select(suggestions[highlight].tag);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
        // Close immediately — option clicks use onMouseDown + preventDefault
        // (below) so they don't blur the input; a delayed close would otherwise
        // risk a setState after unmount when the dialog closes while focused.
        onBlur={() => setOpen(false)}
      />
      {open && suggestions.length > 0 && (
        <ul
          id={listId}
          role="listbox"
          aria-label={t("suggestionsLabel")}
          className="absolute z-50 mt-1 max-h-56 w-full overflow-auto rounded-md border border-gray-200 bg-white py-1 shadow-lg dark:border-gray-700 dark:bg-gray-900"
        >
          {suggestions.map((s, i) => (
            <li
              key={s.tag}
              id={`${listId}-opt-${i}`}
              role="option"
              aria-selected={i === highlight}
              onMouseDown={(e) => {
                e.preventDefault();
                select(s.tag);
              }}
              onMouseEnter={() => setHighlight(i)}
              className={[
                "flex cursor-pointer items-center justify-between px-3 py-1.5 text-sm",
                i === highlight
                  ? "bg-primary/10 text-primary"
                  : "text-gray-700 dark:text-gray-300",
              ].join(" ")}
            >
              <span>{s.tag}</span>
              <span className="text-xs text-gray-400">{s.count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
