"use client";

/**
 * ChannelRoutesEditor (#1759) — per-channel destination contexts of a
 * connector, in the settings dialog next to the ChannelPicker.
 *
 * One row per selected channel, plus one per channel that has a route but is
 * not selected (deselecting never removes a route silently: only choosing
 * Default does). Each row picks Default (the connector's context) / a context
 * / Do not ingest. A switch says whether private channels without a route are
 * ingested at all.
 *
 * The server is the validator (destination readable by you, writable by the
 * connector key's owner); this editor only warns, from the live Slack channel
 * listing: a private channel whose EFFECTIVE destination is a shared context,
 * and a route whose destination was deleted. Channel privacy is never stored,
 * so when the listing is unavailable the editor says privacy is unknown
 * instead of showing no warning.
 *
 * The parent owns the draft (value / onChange) and builds the PATCH with
 * `buildRoutesPatch`; `routeErrorsFromApiError` maps a refusal to its row.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { InlineSpinner } from "@/components/common/LoadingState";
import { ApiError } from "@/lib/api/base";
import { getContexts, type Context } from "@/lib/api/contexts";
import {
  listConnectorChannels,
  type ConnectorChannel,
  type ConnectorChannelRoute,
  type ConnectorChannelRouteInput,
  type UnroutedPrivateChannels,
} from "@/lib/api/workspace-connectors";

/** A channel's destination in the editor. `contextId: null` = a route whose
 * destination is gone (only ever loaded from the server, never chosen). */
export type RouteDestination =
  | { kind: "skip" }
  | { kind: "context"; contextId: string | null };

/** channel id → destination; a channel absent from the map uses Default. */
export type RouteDraft = Record<string, RouteDestination>;

// The listing is cached server-side per page; a workspace with more channels
// than this shows the rest as "privacy unknown" rather than paging forever.
const MAX_LISTING_PAGES = 10;

export function routeDraftFromStored(
  stored: ConnectorChannelRoute[] | undefined,
): RouteDraft {
  const draft: RouteDraft = {};
  for (const route of stored ?? []) {
    draft[route.channel_id] =
      route.action === "route"
        ? { kind: "context", contextId: route.context_id }
        : { kind: "skip" };
  }
  return draft;
}

function routeKey(channelId: string, dest: RouteDestination): string {
  return dest.kind === "skip"
    ? `${channelId}|skip`
    : `${channelId}|route|${dest.contextId ?? ""}`;
}

/**
 * What the settings save sends for the routes: `{}` when nothing changed,
 * the full list (and/or the switch) otherwise, or the channels whose route
 * still has no destination (the save is refused until they get one).
 */
export function buildRoutesPatch(
  draft: RouteDraft,
  unrouted: UnroutedPrivateChannels,
  stored: ConnectorChannelRoute[] | undefined,
  storedUnrouted: UnroutedPrivateChannels | undefined,
):
  | {
      ok: true;
      patch: {
        channel_routes?: ConnectorChannelRouteInput[];
        unrouted_private_channels?: UnroutedPrivateChannels;
      };
    }
  | { ok: false; missing: string[] } {
  const before = new Set(
    Object.entries(routeDraftFromStored(stored)).map(([id, d]) =>
      routeKey(id, d),
    ),
  );
  const entries = Object.entries(draft);
  const after = new Set(entries.map(([id, d]) => routeKey(id, d)));
  const routesChanged =
    before.size !== after.size || [...after].some((k) => !before.has(k));
  const patch: {
    channel_routes?: ConnectorChannelRouteInput[];
    unrouted_private_channels?: UnroutedPrivateChannels;
  } = {};
  if (routesChanged) {
    const missing = entries
      .filter(([, d]) => d.kind === "context" && !d.contextId)
      .map(([id]) => id);
    if (missing.length) return { ok: false, missing };
    patch.channel_routes = entries
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([channel_id, d]) =>
        d.kind === "skip"
          ? { channel_id, action: "skip" }
          : { channel_id, action: "route", context_id: d.contextId as string },
      );
  }
  if (unrouted !== (storedUnrouted ?? "default")) {
    patch.unrouted_private_channels = unrouted;
  }
  return { ok: true, patch };
}

/** Which row a refused save belongs to, and what to say there. */
export function routeErrorsFromApiError(
  err: unknown,
  draft: RouteDraft,
): Record<string, "notFound" | "keyOwner"> {
  if (!(err instanceof ApiError)) return {};
  const errors: Record<string, "notFound" | "keyOwner"> = {};
  const channelId = err.details?.channel_id;
  if (err.status === 422 && typeof channelId === "string" && channelId in draft) {
    errors[channelId] = "keyOwner";
  }
  if (err.status === 404) {
    // The uniform "Context not found: <id>" names the id we sent.
    for (const [id, d] of Object.entries(draft)) {
      if (d.kind === "context" && d.contextId && err.message.includes(d.contextId)) {
        errors[id] = "notFound";
      }
    }
  }
  return errors;
}

function contextLabel(c: Pick<Context, "name" | "display_name">): string {
  return c.display_name || c.name;
}

type Listing =
  | { state: "loading" }
  | { state: "ok"; channels: ConnectorChannel[]; complete: boolean }
  | { state: "unavailable"; channels: ConnectorChannel[] };

export function ChannelRoutesEditor({
  connectorId,
  selectedChannelIds,
  value,
  onChange,
  unrouted,
  onUnroutedChange,
  storedRoutes,
  defaultContextId,
  defaultContextName,
  rowErrors,
}: {
  connectorId: string;
  selectedChannelIds: string[];
  value: RouteDraft;
  onChange: (draft: RouteDraft) => void;
  unrouted: UnroutedPrivateChannels;
  onUnroutedChange: (value: UnroutedPrivateChannels) => void;
  storedRoutes: ConnectorChannelRoute[] | undefined;
  defaultContextId: string | null;
  defaultContextName: string | null;
  rowErrors: Record<string, "notFound" | "keyOwner">;
}) {
  const t = useTranslations("connectors");
  const [contexts, setContexts] = useState<Context[] | null>(null);
  const [contextsFailed, setContextsFailed] = useState(false);
  const [listing, setListing] = useState<Listing>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;
    getContexts()
      .then((resp) => {
        if (!cancelled) setContexts(resp.contexts);
      })
      .catch(() => {
        if (!cancelled) {
          setContexts([]);
          setContextsFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const channels: ConnectorChannel[] = [];
      let cursor: string | undefined;
      try {
        for (let page = 0; page < MAX_LISTING_PAGES; page++) {
          const resp = await listConnectorChannels(connectorId, { cursor });
          channels.push(...resp.channels);
          if ((resp.missing_scopes ?? []).length > 0) {
            // A public-only listing: absent channels may be private.
            if (!cancelled) setListing({ state: "unavailable", channels });
            return;
          }
          if (!resp.next_cursor) {
            if (!cancelled) setListing({ state: "ok", channels, complete: true });
            return;
          }
          cursor = resp.next_cursor;
        }
        if (!cancelled) setListing({ state: "ok", channels, complete: false });
      } catch {
        if (!cancelled) setListing({ state: "unavailable", channels });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connectorId]);

  const byChannel = useMemo(() => {
    const map = new Map<string, ConnectorChannel>();
    if (listing.state !== "loading") {
      for (const c of listing.channels) map.set(c.id, c);
    }
    return map;
  }, [listing]);
  const contextById = useMemo(
    () => new Map((contexts ?? []).map((c) => [c.id, c])),
    [contexts],
  );
  const storedById = useMemo(
    () => new Map((storedRoutes ?? []).map((r) => [r.channel_id, r])),
    [storedRoutes],
  );

  // Selected channels first (in selection order), then routed ones that are
  // not selected — a deselected channel keeps its row until Default.
  const rows = useMemo(() => {
    const ids = [...selectedChannelIds];
    for (const id of Object.keys(value).sort()) {
      if (!ids.includes(id)) ids.push(id);
    }
    return ids;
  }, [selectedChannelIds, value]);

  // Privacy of a destination: the listed context's flag, else what the read
  // model says for a stored route to it (another admin's route to a context
  // this admin cannot list). A context neither knows is another member's
  // private one: shared contexts are always listed to a workspace admin, so
  // "not listed" means private for the shared-context warning.
  const storedPrivacy = useMemo(() => {
    const map = new Map<string, boolean>();
    for (const r of storedRoutes ?? []) {
      if (r.context_id && r.status === "ok" && r.context_is_private !== null) {
        map.set(r.context_id, r.context_is_private);
      }
    }
    return map;
  }, [storedRoutes]);
  const isSharedContext = (contextId: string | null): boolean => {
    if (!contextId) return false;
    const ctx = contextById.get(contextId);
    if (ctx !== undefined) return !ctx.is_private;
    return storedPrivacy.get(contextId) === false;
  };

  const setDestination = (channelId: string, raw: string) => {
    const next = { ...value };
    if (raw === "default") delete next[channelId];
    else if (raw === "skip") next[channelId] = { kind: "skip" };
    else if (raw.startsWith("ctx:")) {
      next[channelId] = { kind: "context", contextId: raw.slice(4) };
    }
    onChange(next);
  };

  const selectValue = (dest: RouteDestination | undefined): string => {
    if (!dest) return "default";
    if (dest.kind === "skip") return "skip";
    return dest.contextId ? `ctx:${dest.contextId}` : "missing";
  };

  const listingUnavailable = listing.state === "unavailable";
  const defaultLabel = defaultContextName
    ? t("routeDefault", { name: defaultContextName })
    : t("routeDefaultUnnamed");

  return (
    <div className="space-y-3" data-testid="channel-routes-editor">
      <div>
        <p className="text-sm font-medium">{t("routesTitle")}</p>
        <p className="text-xs text-muted-foreground">{t("routesHelp")}</p>
      </div>

      {listing.state === "loading" || contexts === null ? (
        <p className="text-xs text-muted-foreground">
          <InlineSpinner aria-hidden="true" /> {t("routesLoading")}
        </p>
      ) : null}

      {contextsFailed && (
        <p role="status" className="text-xs text-muted-foreground">
          {t("routesContextsLoadFailed")}
        </p>
      )}

      {listingUnavailable && (
        // amber-700 / -500: the WCAG AA pair ChannelPicker uses at this size.
        <p role="status" className="text-xs text-amber-700 dark:text-amber-500">
          {t("routesPrivacyUnknown")}
        </p>
      )}

      <ul className="space-y-2">
        {rows.map((channelId) => {
          const dest = value[channelId];
          const channel = byChannel.get(channelId);
          const stored = storedById.get(channelId);
          const current = selectValue(dest);
          const knownContextOption =
            dest?.kind === "context" &&
            dest.contextId !== null &&
            contextById.has(dest.contextId);
          // A stored route to a context this admin cannot list (another
          // admin set it): kept and sent back unchanged unless replaced.
          const unlisted =
            dest?.kind === "context" &&
            !knownContextOption &&
            contexts !== null &&
            stored?.status === "ok" &&
            stored.context_id === dest.contextId;
          const missing =
            dest?.kind === "context" &&
            (dest.contextId === null ||
              (stored?.status === "destination_missing" &&
                stored.context_id === dest.contextId));
          // Effective destination of a private channel: its route, or the
          // default context when it has none and the switch is "default".
          const effectiveShared =
            dest === undefined
              ? unrouted === "default" && isSharedContext(defaultContextId)
              : dest.kind === "context" && isSharedContext(dest.contextId);
          const privateToShared = channel?.is_private === true && effectiveShared;
          // A route into a private context makes it a connector write target:
          // it serves the workspace from now on (informational, never blocks).
          const privateDestination =
            dest?.kind === "context" &&
            dest.contextId !== null &&
            !missing &&
            (contextById.get(dest.contextId)?.is_private ??
              (stored?.status === "ok" &&
                stored.context_id === dest.contextId &&
                stored.context_is_private === true));
          const privacyUnknown =
            channel === undefined && listing.state !== "loading";
          const label = channel
            ? `${channel.is_private ? "🔒 " : "#"}${channel.name}`
            : channelId;
          const error = rowErrors[channelId];

          return (
            <li
              key={channelId}
              data-testid={`route-row-${channelId}`}
              className="space-y-1 rounded-md border p-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-sm">
                  <span className={channel ? undefined : "font-mono"}>{label}</span>
                </span>
                {!selectedChannelIds.includes(channelId) && (
                  <Badge variant="outline" className="shrink-0">
                    {t("routeNotSelected")}
                  </Badge>
                )}
                <div className="w-full sm:w-64">
                  <Select
                    value={current}
                    onValueChange={(v) => setDestination(channelId, v)}
                  >
                    <SelectTrigger
                      aria-label={t("routeDestinationLabel", { channel: label })}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="default">{defaultLabel}</SelectItem>
                      {(contexts ?? []).map((c) => (
                        <SelectItem key={c.id} value={`ctx:${c.id}`}>
                          {`${c.is_private ? "🔒 " : ""}${contextLabel(c)}`}
                        </SelectItem>
                      ))}
                      {dest?.kind === "context" && !knownContextOption && (
                        <SelectItem value={current}>
                          {missing
                            ? t("routeMissingOption")
                            : stored?.context_name ?? dest.contextId ?? ""}
                        </SelectItem>
                      )}
                      <SelectItem value="skip">{t("routeSkip")}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
              {missing && (
                <p role="status" className="text-xs text-amber-700 dark:text-amber-500">
                  {t("routeDestinationMissing")}
                </p>
              )}
              {unlisted && (
                <p className="text-xs text-muted-foreground">
                  {t("routeUnlistedContext")}
                </p>
              )}
              {privateToShared && (
                <p role="status" className="text-xs text-amber-700 dark:text-amber-500">
                  {t("routePrivateToShared")}
                </p>
              )}
              {privateDestination && (
                <p className="text-xs text-muted-foreground">
                  {t("routePrivateDestinationServesWorkspace")}
                </p>
              )}
              {privacyUnknown && !listingUnavailable && (
                <p className="text-xs text-muted-foreground">
                  {t("routeChannelPrivacyUnknown")}
                </p>
              )}
              {error && (
                <p role="alert" className="text-xs text-destructive">
                  {error === "notFound"
                    ? t("routeDestinationNotFound")
                    : t("routeKeyOwnerCannotWrite")}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      <div className="flex items-start gap-3">
        <Switch
          id={`routes-unrouted-${connectorId}`}
          checked={unrouted === "skip"}
          onCheckedChange={(on) => onUnroutedChange(on ? "skip" : "default")}
        />
        <div>
          <label
            htmlFor={`routes-unrouted-${connectorId}`}
            className="text-sm font-medium"
          >
            {t("routesUnroutedPrivateLabel")}
          </label>
          <p className="text-xs text-muted-foreground">
            {t("routesUnroutedPrivateHelp")}
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Flag off (#1759): no editor, but stored routes still apply, so they are
 * shown read-only. Renders nothing when the connector uses no routing.
 */
export function ChannelRoutesSummary({
  routes,
  unrouted,
}: {
  routes: ConnectorChannelRoute[] | undefined;
  unrouted: UnroutedPrivateChannels | undefined;
}) {
  const t = useTranslations("connectors");
  const list = routes ?? [];
  if (list.length === 0 && unrouted !== "skip") return null;
  return (
    <div className="space-y-1" data-testid="channel-routes-summary">
      <p className="text-sm font-medium">{t("routesTitle")}</p>
      <p className="text-xs text-muted-foreground">{t("routesReadOnlyHelp")}</p>
      <ul className="space-y-1 text-sm">
        {list.map((r) => (
          <li key={r.channel_id} className="flex flex-wrap gap-2">
            <span className="font-mono">{r.channel_id}</span>
            <span>
              {r.action === "skip"
                ? t("routeSkip")
                : r.status === "destination_missing"
                  ? t("routeMissingOption")
                  : `${r.context_is_private ? "🔒 " : ""}${r.context_name ?? ""}`}
            </span>
          </li>
        ))}
      </ul>
      {unrouted === "skip" && (
        <p className="text-xs text-muted-foreground">
          {t("routesSummaryUnroutedSkip")}
        </p>
      )}
    </div>
  );
}
