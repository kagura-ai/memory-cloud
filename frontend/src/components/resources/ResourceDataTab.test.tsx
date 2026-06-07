/**
 * Tests for ResourceDataTab (Issue #316).
 *
 * Covers loading → list, empty, error, lazy payload expansion (kv table +
 * raw JSON), the inline payload-size guard, filter apply → refetch, and
 * cursor "Load more" pagination. Mocking shape mirrors
 * IndexerStatusPanel.test.tsx so the resource-tab tests stay a family.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ResourceDataTab } from "./ResourceDataTab";
import type {
  ResourceEventRecord,
  ResourceEventsResponse,
} from "@/lib/api/resources";
import type { ResourceSchema } from "@/lib/api/schemas";

// Stable `t` reference across renders — mirrors real next-intl, which memoizes
// it. An unstable `t` would change fetchPage's useEffect deps and loop forever.
const stableT = (key: string, vars?: Record<string, unknown>) =>
  vars ? `${key}(${JSON.stringify(vars)})` : key;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatDateTime: (iso: string) => `dt(${iso})`,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/hooks/useCopyFeedback", () => ({
  useCopyFeedback: () => ({
    isCopied: () => false,
    copyToTarget: vi.fn().mockResolvedValue(undefined),
  }),
}));

const mockListResourceEvents = vi.fn();
vi.mock("@/lib/api/resources", () => ({
  listResourceEvents: (...args: unknown[]) => mockListResourceEvents(...args),
}));

function makeEvent(
  overrides: Partial<ResourceEventRecord> = {},
): ResourceEventRecord {
  return {
    id: 1,
    op: "upsert",
    doc_id: "sku-1",
    version: 1,
    idempotency_key: null,
    importance: 0.6,
    created_at: "2026-06-07T12:00:00Z",
    payload: { name: "widget", price: 10 },
    event_metadata: {},
    payload_bytes: 30,
    payload_truncated: false,
    ...overrides,
  };
}

function makeResponse(
  overrides: Partial<ResourceEventsResponse> = {},
): ResourceEventsResponse {
  return { events: [makeEvent()], next_cursor: null, ...overrides };
}

const SCHEMA: ResourceSchema = {
  resource_id: "ec-products",
  schema_version: 1,
  field_definitions: [
    {
      name: "name",
      type: "text",
      classification: "public",
      index_hint: "searchable",
      description: "",
      required: true,
    },
  ],
} as ResourceSchema;

beforeEach(() => {
  mockListResourceEvents.mockReset();
});

describe("ResourceDataTab", () => {
  it("loads and lists events newest-first", async () => {
    mockListResourceEvents.mockResolvedValue({
      events: [
        makeEvent({ id: 5, doc_id: "sku-5" }),
        makeEvent({ id: 4, doc_id: "sku-4" }),
      ],
      next_cursor: null,
    });

    render(<ResourceDataTab resourceId="ec-products" schema={null} />);

    await waitFor(() => {
      expect(screen.getByText("sku-5")).toBeInTheDocument();
    });
    expect(screen.getByText("sku-4")).toBeInTheDocument();
    expect(mockListResourceEvents).toHaveBeenCalledWith(
      "ec-products",
      expect.objectContaining({ limit: 20 }),
    );
  });

  it("shows the empty state when there are no events", async () => {
    mockListResourceEvents.mockResolvedValue(
      makeResponse({ events: [], next_cursor: null }),
    );

    render(<ResourceDataTab resourceId="ec-products" schema={null} />);

    await waitFor(() => {
      expect(screen.getByText("emptyTitle")).toBeInTheDocument();
    });
  });

  it("surfaces a fetch error via ErrorBanner", async () => {
    mockListResourceEvents.mockRejectedValue(new Error("boom"));

    render(<ResourceDataTab resourceId="ec-products" schema={null} />);

    await waitFor(() => {
      expect(screen.getByText("boom")).toBeInTheDocument();
    });
  });

  it("lazily renders the payload (kv table + raw JSON) only when expanded", async () => {
    mockListResourceEvents.mockResolvedValue(
      makeResponse({ events: [makeEvent({ id: 7 })] }),
    );

    const { container } = render(
      <ResourceDataTab resourceId="ec-products" schema={SCHEMA} />,
    );

    await waitFor(() => {
      expect(screen.getByText("sku-1")).toBeInTheDocument();
    });
    // Closed: payload not rendered.
    expect(screen.queryByText("payload.raw")).toBeNull();

    // Expand the <details> row.
    const details = container.querySelector("details") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));

    await waitFor(() => {
      expect(
        screen.getByText("payload.raw"),
      ).toBeInTheDocument();
    });
    // Schema-driven kv table shows the field and its searchable hint.
    expect(
      screen.getByText("kv.searchable"),
    ).toBeInTheDocument();
  });

  it("flags an over-cap payload as truncated instead of rendering it", async () => {
    mockListResourceEvents.mockResolvedValue(
      makeResponse({
        events: [
          makeEvent({
            id: 8,
            payload: null,
            payload_truncated: true,
            payload_bytes: 1_100_000,
          }),
        ],
      }),
    );

    const { container } = render(
      <ResourceDataTab resourceId="ec-products" schema={null} />,
    );

    await waitFor(() => {
      expect(screen.getByText("sku-1")).toBeInTheDocument();
    });
    const details = container.querySelector("details") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));

    await waitFor(() => {
      // tooLarge key is interpolated with the {size} var by the mock.
      expect(
        screen.getByText(/payload\.tooLarge/),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("payload.raw")).toBeNull();
  });

  it("re-fetches with the doc_id filter when Apply is clicked", async () => {
    mockListResourceEvents.mockResolvedValue(makeResponse());

    render(<ResourceDataTab resourceId="ec-products" schema={null} />);
    await waitFor(() =>
      expect(mockListResourceEvents).toHaveBeenCalledTimes(1),
    );

    fireEvent.change(screen.getByLabelText("filter.docId"), {
      target: { value: "sku-9" },
    });
    fireEvent.click(screen.getByText("filter.apply"));

    await waitFor(() => {
      expect(mockListResourceEvents).toHaveBeenLastCalledWith(
        "ec-products",
        expect.objectContaining({ doc_id: "sku-9" }),
      );
    });
  });

  it("loads the next page via the cursor and appends results", async () => {
    mockListResourceEvents
      .mockResolvedValueOnce({
        events: [makeEvent({ id: 5, doc_id: "sku-5" })],
        next_cursor: "5",
      })
      .mockResolvedValueOnce({
        events: [makeEvent({ id: 4, doc_id: "sku-4" })],
        next_cursor: null,
      });

    render(<ResourceDataTab resourceId="ec-products" schema={null} />);
    await waitFor(() => expect(screen.getByText("sku-5")).toBeInTheDocument());

    fireEvent.click(screen.getByText("loadMore"));

    await waitFor(() => expect(screen.getByText("sku-4")).toBeInTheDocument());
    // First page still present (appended, not replaced).
    expect(screen.getByText("sku-5")).toBeInTheDocument();
    expect(mockListResourceEvents).toHaveBeenLastCalledWith(
      "ec-products",
      expect.objectContaining({ cursor: "5" }),
    );
  });
});
