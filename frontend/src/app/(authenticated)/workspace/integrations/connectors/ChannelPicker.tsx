"use client";

/**
 * ChannelPicker (#1391) — searchable multi-select for a connector's ingest
 * channels, backed by the server-side Slack channel list. Falls back to
 * manual channel-ID entry on any fetch failure (missing scope, rate limit,
 * transport) so the field is never a dead end. Public channels only in v1;
 * private channels / scope-less legacy installs use the manual lane.
 *
 * The parent owns the selection as an id list; this component is a controlled
 * editor over it (value / onChange), so the existing channel_ids PATCH is
 * unchanged.
 */

import { RefObject, useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Check } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { InlineSpinner } from "@/components/common/LoadingState";
import { cn } from "@/lib/utils/cn";
import {
  listConnectorChannels,
  type ConnectorChannel,
} from "@/lib/api/workspace-connectors";

// Split manual entry on commas/whitespace into a de-duped id list.
export function parseChannelIds(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(/[\s,]+/)
        .map((s) => s.trim())
        .filter(Boolean),
    ),
  );
}

export function ChannelPicker({
  connectorId,
  value,
  onChange,
  inputRef,
}: {
  connectorId: string;
  value: string[];
  onChange: (ids: string[]) => void;
  // Forwarded to whichever text input is active (search or manual) so the
  // "fix channels" affordance can steer initial focus here.
  inputRef?: RefObject<HTMLInputElement | null>;
}) {
  const t = useTranslations("connectors");
  const [mode, setMode] = useState<"select" | "manual">("select");
  const [channels, setChannels] = useState<ConnectorChannel[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  // Set once the picker had to fall back (scope/rate/transport) so the copy can
  // tell the admin why they're typing IDs.
  const [fellBack, setFellBack] = useState(false);

  const selected = new Set(value);

  const load = useCallback(
    async (next?: string) => {
      setLoading(true);
      try {
        const page = await listConnectorChannels(connectorId, {
          cursor: next,
        });
        setChannels((prev) =>
          next ? [...(prev ?? []), ...page.channels] : page.channels,
        );
        setCursor(page.next_cursor);
      } catch {
        // Any failure → manual lane (never a dead field). #1391 fallback.
        setFellBack(true);
        setMode("manual");
      } finally {
        setLoading(false);
      }
    },
    [connectorId],
  );

  useEffect(() => {
    if (mode === "select" && channels === null && !loading) {
      void load();
    }
  }, [mode, channels, loading, load]);

  const toggle = (id: string) => {
    const nextSet = new Set(value);
    if (nextSet.has(id)) nextSet.delete(id);
    else nextSet.add(id);
    onChange(Array.from(nextSet));
  };

  if (mode === "manual") {
    return (
      <div className="space-y-1">
        <Input
          ref={inputRef}
          aria-label={t("channelsLabel")}
          placeholder="C0123ABC456, C0456DEF789"
          value={value.join(", ")}
          onChange={(e) => onChange(parseChannelIds(e.target.value))}
        />
        <p className="text-xs text-muted-foreground">
          {fellBack ? t("channelsPickerFallback") : t("channelsHelp")}
        </p>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setFellBack(false);
            setMode("select");
          }}
        >
          {t("channelsModeSelect")}
        </Button>
      </div>
    );
  }

  const visible = (channels ?? []).filter((c) =>
    query ? c.name.toLowerCase().includes(query.toLowerCase()) : true,
  );
  // Selected ids not present in the fetched list (e.g. private channels) —
  // shown as removable chips so a save never silently drops them.
  const listedIds = new Set((channels ?? []).map((c) => c.id));
  const unlisted = value.filter((id) => !listedIds.has(id));
  // #1451: selections that Slack will never deliver events for. Counted only
  // over channels we actually fetched — an unlisted id (private channel, manual
  // entry) has unknown membership, and claiming it is not ingesting would be a
  // guess dressed as a fact.
  const notJoinedCount = (channels ?? []).filter(
    (c) => selected.has(c.id) && !c.is_member,
  ).length;
  // …but silence about the unknown ones would repeat the very failure this
  // issue is about. A selected id we have not fetched yet (it lives on a later
  // Slack page) is unverified, not verified-fine, so say so instead of letting
  // the absence of a warning read as an all-clear (review finding).
  const unverifiedCount = cursor !== null ? unlisted.length : 0;

  return (
    <div className="space-y-2">
      <Input
        ref={inputRef}
        aria-label={t("channelsSearchLabel")}
        placeholder={t("channelsSearchPlaceholder")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {channels !== null && unlisted.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {unlisted.map((id) => (
            <Badge key={id} variant="secondary" className="gap-1">
              <span className="font-mono">{id}</span>
              <button
                type="button"
                aria-label={t("channelsRemove", { id })}
                onClick={() => toggle(id)}
                className="ml-0.5"
              >
                ×
              </button>
            </Badge>
          ))}
        </div>
      )}

      {notJoinedCount > 0 && (
        // amber-700 (not -600) clears WCAG AA at this 12px size on the light
        // background; -500 is the dark-theme counterpart (review finding).
        <p role="status" className="text-xs text-amber-700 dark:text-amber-500">
          {t("channelsNotJoinedWarning", { count: notJoinedCount })}
        </p>
      )}

      {unverifiedCount > 0 && (
        <p role="status" className="text-xs text-muted-foreground">
          {t("channelsMembershipUnverified", { count: unverifiedCount })}
        </p>
      )}

      <div className="max-h-48 overflow-y-auto rounded-md border">
        {channels === null && loading ? (
          <div className="p-3">
            <InlineSpinner aria-hidden="true" /> {t("channelsLoading")}
          </div>
        ) : visible.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">
            {t("channelsPickerEmpty")}
          </p>
        ) : (
          <ul>
            {visible.map((c) => {
              const on = selected.has(c.id);
              return (
                <li key={c.id}>
                  <button
                    type="button"
                    onClick={() => toggle(c.id)}
                    aria-pressed={on}
                    className={cn(
                      "flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-accent",
                      on && "font-medium",
                    )}
                  >
                    <span
                      className={cn(
                        "flex h-4 w-4 items-center justify-center rounded border",
                        on && "bg-primary text-primary-foreground",
                      )}
                    >
                      {on && <Check className="h-3 w-3" />}
                    </span>
                    <span className="truncate">
                      {c.is_private ? "🔒 " : "#"}
                      {c.name}
                    </span>
                    {!c.is_member && (
                      <Badge
                        variant="outline"
                        className="ml-auto shrink-0 text-muted-foreground"
                        title={t("channelsBotNotInChannelHint")}
                      >
                        {t("channelsBotNotInChannel")}
                      </Badge>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="flex items-center justify-between">
        {cursor ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={loading}
            onClick={() => void load(cursor)}
          >
            {loading ? <InlineSpinner aria-hidden="true" /> : null}
            {t("channelsLoadMore")}
          </Button>
        ) : (
          <span />
        )}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setMode("manual")}
        >
          {t("channelsModeManual")}
        </Button>
      </div>
    </div>
  );
}
