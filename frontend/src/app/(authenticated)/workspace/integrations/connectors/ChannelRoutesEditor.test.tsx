/**
 * ChannelRoutesEditor (#1759): per-channel destinations in the connector
 * settings dialog — rows, warnings, the switch, the read-only summary, and
 * the request the save builds.
 *
 * Rendered with the REAL en / ja catalogues (the MissingScopesNotice.test.tsx
 * pattern) so a missing key or ICU argument fails here.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useState, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { ApiError } from "@/lib/api/base";
import type {
  ConnectorChannel,
  ConnectorChannelRoute,
  UnroutedPrivateChannels,
} from "@/lib/api/workspace-connectors";

const mockListConnectorChannels = vi.fn();
vi.mock("@/lib/api/workspace-connectors", () => ({
  listConnectorChannels: (...args: unknown[]) => mockListConnectorChannels(...args),
}));
const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...args: unknown[]) => mockGetContexts(...args),
}));

// Radix Select uses pointer events jsdom does not fire; a native <select>
// keeps the value / onValueChange contract (the signup-gate test pattern).
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange?: (v: string) => void;
    children: ReactNode;
  }) => (
    <select value={value} onChange={(e) => onValueChange?.(e.target.value)}>
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
    <option value={value}>{children}</option>
  ),
}));

import {
  ChannelRoutesEditor,
  ChannelRoutesSummary,
  buildRoutesPatch,
  routeDraftFromStored,
  routeErrorsFromApiError,
  type RouteDraft,
} from "./ChannelRoutesEditor";

const CATALOGUES = { en, ja } as const;
type Locale = keyof typeof CATALOGUES;

let intlErrors: string[] = [];

beforeEach(() => {
  intlErrors = [];
  mockListConnectorChannels.mockReset();
  mockGetContexts.mockReset();
  mockGetContexts.mockResolvedValue({
    contexts: [
      { id: "ctx-default", name: "general", display_name: null, is_private: false },
      { id: "ctx-shared", name: "sales", display_name: "Sales", is_private: false },
      { id: "ctx-private", name: "hr", display_name: "HR", is_private: true },
    ],
  });
  mockListConnectorChannels.mockResolvedValue({
    channels: CHANNELS,
    next_cursor: null,
    missing_scopes: [],
  });
});

afterEach(() => {
  cleanup();
  expect(intlErrors).toEqual([]);
});

const CHANNELS: ConnectorChannel[] = [
  { id: "C0PUB", name: "random", is_private: false, is_member: true },
  { id: "C0PRIV", name: "secret", is_private: true, is_member: true },
];

function renderIn(locale: Locale, ui: ReactElement) {
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={CATALOGUES[locale] as Record<string, unknown>}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${locale} ${error.code}: ${error.message}`);
      }}
    >
      {ui}
    </NextIntlClientProvider>,
  );
}

const onChangeSpy = vi.fn();
const onUnroutedSpy = vi.fn();

function Harness({
  selected,
  stored = [],
  initialUnrouted = "default",
  rowErrors = {},
  defaultContextId = "ctx-default",
}: {
  selected: string[];
  stored?: ConnectorChannelRoute[];
  initialUnrouted?: UnroutedPrivateChannels;
  rowErrors?: Record<string, "notFound" | "keyOwner">;
  defaultContextId?: string | null;
}) {
  const [draft, setDraft] = useState<RouteDraft>(routeDraftFromStored(stored));
  const [unrouted, setUnrouted] = useState<UnroutedPrivateChannels>(initialUnrouted);
  return (
    <ChannelRoutesEditor
      connectorId="conn-1"
      selectedChannelIds={selected}
      value={draft}
      onChange={(d) => {
        onChangeSpy(d);
        setDraft(d);
      }}
      unrouted={unrouted}
      onUnroutedChange={(v) => {
        onUnroutedSpy(v);
        setUnrouted(v);
      }}
      storedRoutes={stored}
      defaultContextId={defaultContextId}
      defaultContextName="general"
      rowErrors={rowErrors}
    />
  );
}

function row(channelId: string) {
  return within(screen.getByTestId(`route-row-${channelId}`));
}

async function settled() {
  await waitFor(() => expect(screen.queryByText(en.connectors.routesLoading)).toBeNull());
}

const route = (over: Partial<ConnectorChannelRoute>): ConnectorChannelRoute => ({
  channel_id: "C0X",
  action: "route",
  context_id: "ctx-shared",
  context_name: "Sales",
  context_is_private: false,
  status: "ok",
  ...over,
});

describe("ChannelRoutesEditor", () => {
  it("shows a row per selected channel and keeps a deselected routed one", async () => {
    renderIn(
      "en",
      <Harness
        selected={["C0PUB"]}
        stored={[route({ channel_id: "C0OLD", action: "skip", context_id: null })]}
      />,
    );
    await settled();
    expect(row("C0PUB").getByText("#random")).toBeInTheDocument();
    expect(row("C0OLD").getByText("Not selected")).toBeInTheDocument();
    expect(row("C0OLD").getByRole("combobox")).toHaveValue("skip");

    // Only choosing Default removes the route — and with it the row.
    fireEvent.change(row("C0OLD").getByRole("combobox"), { target: { value: "default" } });
    expect(onChangeSpy).toHaveBeenLastCalledWith({});
    expect(screen.queryByTestId("route-row-C0OLD")).toBeNull();
  });

  it("routes a channel to a context and back to Default", async () => {
    renderIn("en", <Harness selected={["C0PUB"]} />);
    await settled();
    const select = row("C0PUB").getByRole("combobox");
    expect(select).toHaveValue("default");
    expect(row("C0PUB").getByRole("option", { name: "Default (general)" })).toBeInTheDocument();
    expect(row("C0PUB").getByRole("option", { name: "🔒 HR" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "ctx:ctx-private" } });
    expect(onChangeSpy).toHaveBeenLastCalledWith({
      C0PUB: { kind: "context", contextId: "ctx-private" },
    });
    fireEvent.change(select, { target: { value: "default" } });
    expect(onChangeSpy).toHaveBeenLastCalledWith({});
  });

  it("warns when a private channel's effective destination is a shared context", async () => {
    renderIn("en", <Harness selected={["C0PRIV", "C0PUB"]} />);
    await settled();
    const warning = en.connectors.routePrivateToShared;
    // No route + switch "default" → the default context, which is shared.
    expect(row("C0PRIV").getByText(warning)).toBeInTheDocument();
    expect(row("C0PUB").queryByText(warning)).toBeNull();

    fireEvent.change(row("C0PRIV").getByRole("combobox"), {
      target: { value: "ctx:ctx-private" },
    });
    expect(row("C0PRIV").queryByText(warning)).toBeNull();
    fireEvent.change(row("C0PRIV").getByRole("combobox"), {
      target: { value: "ctx:ctx-shared" },
    });
    expect(row("C0PRIV").getByText(warning)).toBeInTheDocument();
  });

  it("does not warn for an unrouted private channel once the switch skips it", async () => {
    renderIn("en", <Harness selected={["C0PRIV"]} />);
    await settled();
    expect(row("C0PRIV").getByText(en.connectors.routePrivateToShared)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch"));
    expect(onUnroutedSpy).toHaveBeenLastCalledWith("skip");
    expect(row("C0PRIV").queryByText(en.connectors.routePrivateToShared)).toBeNull();
  });

  it("treats a default context the admin cannot list as private (no warning)", async () => {
    renderIn("en", <Harness selected={["C0PRIV"]} defaultContextId="ctx-someone-elses" />);
    await settled();
    expect(row("C0PRIV").queryByText(en.connectors.routePrivateToShared)).toBeNull();
  });

  it("flags a deleted destination", async () => {
    renderIn(
      "en",
      <Harness
        selected={["C0PUB"]}
        stored={[
          route({
            channel_id: "C0PUB",
            context_id: "ctx-gone",
            context_name: null,
            context_is_private: null,
            status: "destination_missing",
          }),
          route({
            channel_id: "C0HARD",
            context_id: null,
            context_name: null,
            context_is_private: null,
            status: "destination_missing",
          }),
        ]}
      />,
    );
    await settled();
    for (const id of ["C0PUB", "C0HARD"]) {
      expect(row(id).getByText(en.connectors.routeDestinationMissing)).toBeInTheDocument();
      expect(row(id).getByRole("option", { name: "Missing destination" })).toBeInTheDocument();
    }
  });

  it("keeps another admin's route to a context this admin cannot list", async () => {
    const stored = [
      route({
        channel_id: "C0PUB",
        context_id: "ctx-theirs",
        context_name: "Their notes",
        context_is_private: true,
      }),
      route({
        channel_id: "C0PRIV",
        context_id: "ctx-their-shared",
        context_name: "Their shared",
        context_is_private: false,
      }),
    ];
    renderIn("en", <Harness selected={["C0PUB", "C0PRIV"]} stored={stored} />);
    await settled();
    // Rendered as selected, under the read model's name, read-only note.
    expect(row("C0PUB").getByRole("combobox")).toHaveValue("ctx:ctx-theirs");
    expect(row("C0PUB").getByRole("option", { name: "Their notes" })).toBeInTheDocument();
    expect(row("C0PUB").getByText(en.connectors.routeUnlistedContext)).toBeInTheDocument();
    // Its privacy comes from the read model: private channel → shared context.
    expect(row("C0PRIV").getByText(en.connectors.routePrivateToShared)).toBeInTheDocument();

    // Changing another row sends the unlisted route back unchanged.
    fireEvent.change(row("C0PRIV").getByRole("combobox"), { target: { value: "skip" } });
    const draft = onChangeSpy.mock.lastCall?.[0] as RouteDraft;
    expect(buildRoutesPatch(draft, "default", stored, "default")).toEqual({
      ok: true,
      patch: {
        channel_routes: [
          { channel_id: "C0PRIV", action: "skip" },
          { channel_id: "C0PUB", action: "route", context_id: "ctx-theirs" },
        ],
      },
    });
    // ...and it can still be replaced.
    fireEvent.change(row("C0PUB").getByRole("combobox"), { target: { value: "default" } });
    expect(onChangeSpy.mock.lastCall?.[0]).toEqual({ C0PRIV: { kind: "skip" } });
  });

  it("says channel privacy is unknown when the listing fails", async () => {
    mockListConnectorChannels.mockRejectedValue(new Error("missing scope"));
    renderIn("en", <Harness selected={["C0PRIV"]} />);
    await settled();
    expect(screen.getByText(en.connectors.routesPrivacyUnknown)).toBeInTheDocument();
    expect(row("C0PRIV").getByText("C0PRIV")).toBeInTheDocument();
  });

  it("says channel privacy is unknown for a public-only listing", async () => {
    mockListConnectorChannels.mockResolvedValue({
      channels: [CHANNELS[0]],
      next_cursor: null,
      missing_scopes: ["groups:read"],
    });
    renderIn("en", <Harness selected={["C0PUB"]} />);
    await settled();
    expect(screen.getByText(en.connectors.routesPrivacyUnknown)).toBeInTheDocument();
  });

  it("marks a channel missing from a complete listing as privacy unknown", async () => {
    renderIn("en", <Harness selected={["C0ELSEWHERE"]} />);
    await settled();
    expect(
      row("C0ELSEWHERE").getByText(en.connectors.routeChannelPrivacyUnknown),
    ).toBeInTheDocument();
  });

  it("shows a server refusal next to its row", async () => {
    renderIn(
      "en",
      <Harness
        selected={["C0PUB", "C0PRIV"]}
        stored={[route({ channel_id: "C0PUB" }), route({ channel_id: "C0PRIV" })]}
        rowErrors={{ C0PUB: "notFound", C0PRIV: "keyOwner" }}
      />,
    );
    await settled();
    expect(row("C0PUB").getByRole("alert")).toHaveTextContent(
      en.connectors.routeDestinationNotFound,
    );
    expect(row("C0PRIV").getByRole("alert")).toHaveTextContent(
      en.connectors.routeKeyOwnerCannotWrite,
    );
  });

  it("says a newly chosen private destination passes to the workspace", async () => {
    const notice = en.connectors.routePrivateDestinationServesWorkspace;
    renderIn("en", <Harness selected={["C0PUB", "C0PRIV"]} />);
    await settled();
    // Default and Do not ingest: no notice.
    expect(screen.queryByText(notice)).toBeNull();
    fireEvent.change(row("C0PUB").getByRole("combobox"), { target: { value: "skip" } });
    expect(screen.queryByText(notice)).toBeNull();
    // A shared context: no notice.
    fireEvent.change(row("C0PUB").getByRole("combobox"), {
      target: { value: "ctx:ctx-shared" },
    });
    expect(screen.queryByText(notice)).toBeNull();
    // A private context: the notice, on that row only, while the other row
    // keeps its private-to-shared warning.
    fireEvent.change(row("C0PUB").getByRole("combobox"), {
      target: { value: "ctx:ctx-private" },
    });
    expect(row("C0PUB").getByText(notice)).toBeInTheDocument();
    expect(row("C0PRIV").queryByText(notice)).toBeNull();
    expect(row("C0PRIV").getByText(en.connectors.routePrivateToShared)).toBeInTheDocument();
  });

  it("says a stored private destination passes to the workspace, next to the other notes", async () => {
    const notice = en.connectors.routePrivateDestinationServesWorkspace;
    renderIn(
      "en",
      <Harness
        selected={["C0ELSEWHERE", "C0PUB"]}
        stored={[
          // Another admin's route to a private context this admin cannot list.
          route({
            channel_id: "C0ELSEWHERE",
            context_id: "ctx-theirs",
            context_name: "Their notes",
            context_is_private: true,
          }),
          route({
            channel_id: "C0PUB",
            context_id: "ctx-gone",
            context_name: null,
            context_is_private: null,
            status: "destination_missing",
          }),
        ]}
        rowErrors={{ C0ELSEWHERE: "keyOwner" }}
      />,
    );
    await settled();
    const theirs = row("C0ELSEWHERE");
    expect(theirs.getByText(notice)).toBeInTheDocument();
    expect(theirs.getByText(en.connectors.routeUnlistedContext)).toBeInTheDocument();
    expect(theirs.getByText(en.connectors.routeChannelPrivacyUnknown)).toBeInTheDocument();
    expect(theirs.getByRole("alert")).toHaveTextContent(en.connectors.routeKeyOwnerCannotWrite);
    // A missing destination keeps its error and gets no notice.
    expect(row("C0PUB").getByText(en.connectors.routeDestinationMissing)).toBeInTheDocument();
    expect(row("C0PUB").queryByText(notice)).toBeNull();
  });

  it("says a private destination passes to the workspace in Japanese", async () => {
    renderIn("ja", <Harness selected={["C0PUB"]} />);
    await settled();
    fireEvent.change(row("C0PUB").getByRole("combobox"), {
      target: { value: "ctx:ctx-private" },
    });
    expect(
      row("C0PUB").getByText(ja.connectors.routePrivateDestinationServesWorkspace),
    ).toBeInTheDocument();
  });

  it("renders in Japanese", async () => {
    renderIn("ja", <Harness selected={["C0PRIV"]} />);
    await settled();
    expect(screen.getByText(ja.connectors.routesTitle)).toBeInTheDocument();
    expect(row("C0PRIV").getByText(ja.connectors.routePrivateToShared)).toBeInTheDocument();
    expect(row("C0PRIV").getByRole("option", { name: "既定（general）" })).toBeInTheDocument();
    expect(screen.getByText(ja.connectors.routesUnroutedPrivateLabel)).toBeInTheDocument();
  });
});

describe("ChannelRoutesSummary (flag off)", () => {
  it("renders nothing for a connector without routing", () => {
    renderIn("en", <ChannelRoutesSummary routes={[]} unrouted="default" />);
    expect(screen.queryByTestId("channel-routes-summary")).toBeNull();
  });

  it.each(["en", "ja"] as const)("lists stored routes read-only (%s)", (locale) => {
    renderIn(
      locale,
      <ChannelRoutesSummary
        routes={[
          route({ channel_id: "C01" }),
          route({ channel_id: "C02", action: "skip", context_id: null }),
          route({ channel_id: "C03", status: "destination_missing", context_name: null }),
        ]}
        unrouted="skip"
      />,
    );
    const summary = screen.getByTestId("channel-routes-summary");
    expect(within(summary).getByText("Sales")).toBeInTheDocument();
    expect(within(summary).getByText(CATALOGUES[locale].connectors.routeSkip)).toBeInTheDocument();
    expect(
      within(summary).getByText(CATALOGUES[locale].connectors.routeMissingOption),
    ).toBeInTheDocument();
    expect(
      within(summary).getByText(CATALOGUES[locale].connectors.routesSummaryUnroutedSkip),
    ).toBeInTheDocument();
    expect(within(summary).queryByRole("combobox")).toBeNull();
  });
});

describe("buildRoutesPatch", () => {
  const stored = [route({ channel_id: "C01" }), route({ channel_id: "C02", action: "skip", context_id: null })];

  it("sends nothing when nothing changed", () => {
    expect(buildRoutesPatch(routeDraftFromStored(stored), "default", stored, "default")).toEqual({
      ok: true,
      patch: {},
    });
  });

  it("sends the full list, sorted, when one route changed", () => {
    const draft = { ...routeDraftFromStored(stored), C00: { kind: "skip" as const } };
    expect(buildRoutesPatch(draft, "default", stored, "default")).toEqual({
      ok: true,
      patch: {
        channel_routes: [
          { channel_id: "C00", action: "skip" },
          { channel_id: "C01", action: "route", context_id: "ctx-shared" },
          { channel_id: "C02", action: "skip" },
        ],
      },
    });
  });

  it("sends an empty list when every route went back to Default", () => {
    expect(buildRoutesPatch({}, "default", stored, "default")).toEqual({
      ok: true,
      patch: { channel_routes: [] },
    });
  });

  it("sends the switch alone when only it changed", () => {
    expect(buildRoutesPatch(routeDraftFromStored(stored), "skip", stored, "default")).toEqual({
      ok: true,
      patch: { unrouted_private_channels: "skip" },
    });
  });

  it("refuses to send a route that still has no destination", () => {
    const withMissing = [
      route({ channel_id: "C09", context_id: null, status: "destination_missing" }),
    ];
    const draft = { ...routeDraftFromStored(withMissing), C10: { kind: "skip" as const } };
    expect(buildRoutesPatch(draft, "default", withMissing, "default")).toEqual({
      ok: false,
      missing: ["C09"],
    });
  });
});

describe("routeErrorsFromApiError", () => {
  const draft: RouteDraft = {
    C01: { kind: "context", contextId: "ctx-a" },
    C02: { kind: "context", contextId: "ctx-b" },
  };

  it("maps the uniform 404 to the row whose context it names", () => {
    const err = new ApiError({ status: 404, error: "RES-001", message: "Context not found: ctx-b" });
    expect(routeErrorsFromApiError(err, draft)).toEqual({ C02: "notFound" });
  });

  it("maps the key-owner refusal to its channel", () => {
    const err = new ApiError({
      status: 422,
      error: "VAL-001",
      message: "channel_routes: channel C01 cannot be routed there.",
      details: { field: "channel_routes", channel_id: "C01" },
    });
    expect(routeErrorsFromApiError(err, draft)).toEqual({ C01: "keyOwner" });
  });

  it("ignores anything else", () => {
    expect(routeErrorsFromApiError(new Error("boom"), draft)).toEqual({});
    expect(
      routeErrorsFromApiError(new ApiError({ status: 409, message: "stale" }), draft),
    ).toEqual({});
  });
});
