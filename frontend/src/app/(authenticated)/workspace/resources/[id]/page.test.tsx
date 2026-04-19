/**
 * Tests for the Resource detail page.
 *
 * Verifies:
 * - stats strip renders on success and the Schemas tab shows the schema table
 * - schema absence (ApiError 404) renders the "no schema" EmptyState in the
 *   Schemas tab with a "Create schema" action
 * - non-404 getSchema errors surface via ErrorBanner (not silenced)
 * - listResources() and getSchema() run in parallel (both start before either resolves)
 * - unknown resource_id renders ErrorBanner + back link
 * - all five tabs (overview / data / schemas / tokens / events) are exposed
 * - the route `id` from useParams() is used directly (App Router already decodes)
 * - resource_ids containing a literal `%` do not throw URIError
 * - Pro-plan gate + workspaceReady hydration guard
 *
 * `getIndexerStatus` is also called once the resource resolves; mocked here
 * to a benign empty payload so the IndexerStatusPanel renders without
 * coupling these tests to its surface.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";

import ResourceDetailPage from "./page";
import { ApiError } from "@/lib/api/base";

// ---------- Mocks ------------------------------------------------------------

const mockListResources = vi.fn();
const mockGetSchema = vi.fn();
const mockGetIndexerStatus = vi.fn();
const mockRouterReplace = vi.fn();
const mockRouterPush = vi.fn();

let mockParamsId = "ec_products";
let mockSearchParams = new URLSearchParams();
let mockCurrentWorkspace: {
  plan_name?: string;
  current_user_role?: string;
} | null = null;

vi.mock("@/lib/api/resources", () => ({
  listResources: (...args: unknown[]) => mockListResources(...args),
  getIndexerStatus: (...args: unknown[]) => mockGetIndexerStatus(...args),
}));

vi.mock("@/lib/api/schemas", () => ({
  getSchema: (...args: unknown[]) => mockGetSchema(...args),
}));

// Heavy in-tab components are stubbed — they own their own coverage and
// aren't part of this page-level contract.
vi.mock("@/components/credentials/ResourceTokensTabPanel", () => ({
  ResourceTokensTabPanel: ({
    resourceIdFilter,
  }: {
    resourceIdFilter?: string;
  }) => <div data-testid="tokens-panel" data-filter={resourceIdFilter} />,
}));

vi.mock("@/components/resources/IndexerStatusPanel", () => ({
  IndexerStatusPanel: () => <div data-testid="indexer-panel" />,
}));

// Props the dialog was last opened with — captured so tests can assert the
// wiring (lockedResourceId, pre-fill fields) without rendering the real dialog.
// Reset in beforeEach so each test starts from a clean slate.
let capturedCreateSchemaDialogProps: Record<string, unknown> | null = null;
vi.mock("@/components/schemas/CreateSchemaDialog", () => ({
  CreateSchemaDialog: (props: Record<string, unknown>) => {
    if (props.isOpen) {
      capturedCreateSchemaDialogProps = props;
    }
    return props.isOpen ? (
      <div role="dialog" data-testid="create-schema-dialog" />
    ) : null;
  },
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: mockParamsId }),
  useSearchParams: () => mockSearchParams,
  usePathname: () => "/workspace/resources/ec_products",
  useRouter: () => ({ replace: mockRouterReplace, push: mockRouterPush }),
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: mockCurrentWorkspace }),
}));

// Stable translator — a new function reference each render would invalidate
// useCallback([t]) and re-fire the fetch effect every render (infinite loop).
const makeTranslator = (namespace: string) => {
  const prefix = namespace ? `${namespace}.` : "";
  return (key: string, values?: Record<string, unknown>) => {
    if (values && Object.keys(values).length > 0) {
      return `${prefix}${key}:${JSON.stringify(values)}`;
    }
    return `${prefix}${key}`;
  };
};
const translatorCache = new Map<string, ReturnType<typeof makeTranslator>>();
vi.mock("next-intl", () => ({
  useTranslations: (namespace?: string) => {
    const ns = namespace ?? "";
    if (!translatorCache.has(ns)) {
      translatorCache.set(ns, makeTranslator(ns));
    }
    return translatorCache.get(ns)!;
  },
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `rel(${iso})`,
  formatDateTime: (iso: string) => `dt(${iso})`,
}));

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));

// ---------- Fixtures ---------------------------------------------------------

const makeResource = (overrides = {}) => ({
  resource_id: "ec_products",
  context_id: "550e8400-e29b-41d4-a716-446655440000",
  context_name: "ec-products",
  context_display_name: "EC Products",
  token_count: 2,
  memory_count: 47,
  current_schema_version: 3,
  created_at: "2026-03-01T00:00:00Z",
  updated_at: "2026-04-14T09:15:30Z",
  ...overrides,
});

const makeSchema = (overrides = {}) => ({
  resource_id: "ec_products",
  schema_version: 3,
  field_definitions: [
    {
      name: "title",
      type: "text" as const,
      description: "Product title",
      classification: "public" as const,
      index_hint: "fulltext",
      required: true,
    },
  ],
  created_at: "2026-03-01T00:00:00Z",
  ...overrides,
});

beforeEach(() => {
  mockListResources.mockReset();
  mockGetSchema.mockReset();
  mockGetIndexerStatus.mockReset();
  // Default to a benign empty payload so IndexerStatusPanel does not error
  // and tests that don't exercise it stay focused.
  mockGetIndexerStatus.mockResolvedValue({
    resource_id: "ec_products",
    state: null,
    recent_events: [],
  });
  mockRouterReplace.mockReset();
  mockRouterPush.mockReset();
  mockParamsId = "ec_products";
  mockSearchParams = new URLSearchParams();
  mockCurrentWorkspace = { plan_name: "pro", current_user_role: "owner" };
  capturedCreateSchemaDialogProps = null;
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("ResourceDetailPage", () => {
  it("renders stats strip on success and exposes schema content under the Schemas tab", async () => {
    // Open the page already on the Schemas tab — Radix Tabs only mounts the
    // active tabpanel, so we can't grep schema content while Overview is up.
    mockSearchParams = new URLSearchParams("tab=schemas");
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockResolvedValue(makeSchema());

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("EC Products")).toBeInTheDocument();
    });
    expect(screen.getByText("title")).toBeInTheDocument();
    expect(screen.getByText(/versionLabel.*"version":3/)).toBeInTheDocument();
  });

  it("renders 'no schema' empty state with a Create action when getSchema returns ApiError(404)", async () => {
    mockSearchParams = new URLSearchParams("tab=schemas");
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "Not found", status: 404 }),
    );

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(
        screen.getByText("resources.schema.emptyTitle"),
      ).toBeInTheDocument();
    });
    // The action wiring is the #326 deliverable — pin that the actionable
    // button is present, not just the empty-state copy.
    expect(
      screen.getByRole("button", { name: "resources.schema.createAction" }),
    ).toBeInTheDocument();
  });

  it("opens CreateSchemaDialog with lockedResourceId when the EmptyState Create action is clicked", async () => {
    // Guards against a regression where the picker would be shown and the
    // operator could write the new schema against a different resource —
    // page.tsx pins the dialog to this page's resource via lockedResourceId.
    mockSearchParams = new URLSearchParams("tab=schemas");
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "Not found", status: 404 }),
    );

    render(<ResourceDetailPage />);

    const createButton = await screen.findByRole("button", {
      name: "resources.schema.createAction",
    });
    fireEvent.click(createButton);

    expect(
      await screen.findByTestId("create-schema-dialog"),
    ).toBeInTheDocument();
    expect(capturedCreateSchemaDialogProps).not.toBeNull();
    expect(capturedCreateSchemaDialogProps?.lockedResourceId).toBe(
      "ec_products",
    );
  });

  it("opens CreateSchemaDialog with lockedResourceId when 'Create new version' is clicked", async () => {
    // Regression guard for the v1=title / v2=price-only field-loss bug seen
    // during #326 dev: schemas are immutable per version, so the "new version"
    // dialog MUST end up pre-filled with the current version's fields. The
    // dialog owns the pre-fill fetch (keyed on lockedResourceId); this page
    // test pins the page-side contract — that the "new version" button opens
    // the dialog with lockedResourceId set to the current resource. Without
    // that, the dialog cannot know which resource to pre-fill from.
    mockSearchParams = new URLSearchParams("tab=schemas");
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockResolvedValue(makeSchema());

    render(<ResourceDetailPage />);

    const newVersionButton = await screen.findByRole("button", {
      name: "resources.schema.createNewVersionAction",
    });
    fireEvent.click(newVersionButton);

    expect(
      await screen.findByTestId("create-schema-dialog"),
    ).toBeInTheDocument();
    expect(capturedCreateSchemaDialogProps).not.toBeNull();
    expect(capturedCreateSchemaDialogProps?.lockedResourceId).toBe(
      "ec_products",
    );
  });

  it("surfaces non-404 getSchema errors via ErrorBanner (not silenced as 'no schema')", async () => {
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "Internal server error", status: 500 }),
    );

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Internal server error",
      );
    });
  });

  it("fetches list and schema in parallel", async () => {
    let listStarted = false;
    let schemaStarted = false;
    let resolveList: (value: unknown) => void = () => {};
    let resolveSchema: (value: unknown) => void = () => {};

    mockListResources.mockImplementation(() => {
      listStarted = true;
      return new Promise((resolve) => {
        resolveList = resolve;
      });
    });
    mockGetSchema.mockImplementation(() => {
      schemaStarted = true;
      return new Promise((resolve) => {
        resolveSchema = resolve;
      });
    });

    render(<ResourceDetailPage />);

    // Let the effect run its microtasks
    await Promise.resolve();
    await Promise.resolve();

    // Both must have started before either resolves — proves parallelism.
    // (The indexer-status fetch fires only after the resource resolves and
    // is intentionally outside the parallel-pair contract this test pins.)
    expect(listStarted).toBe(true);
    expect(schemaStarted).toBe(true);

    resolveList({ resources: [makeResource()], total: 1 });
    resolveSchema(makeSchema());
    await waitFor(() => {
      expect(screen.getByText("EC Products")).toBeInTheDocument();
    });
  });

  it("renders notFoundTitle when resource_id is not in the workspace list", async () => {
    mockParamsId = "missing_resource";
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "Not found", status: 404 }),
    );

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
    // Not-found uses the dedicated title, not the generic "Resource" title
    expect(
      screen.getByText("resources.detail.notFoundTitle"),
    ).toBeInTheDocument();
    expect(screen.getByText("resources.detail.backToList")).toBeInTheDocument();
  });

  it("renders generic title (not notFoundTitle) when a real fetch fails", async () => {
    // Regression: a server error used to set error=notFound, making
    // isFetchError true AND rendering the generic title — masking the
    // real "backend error" intent as "resource doesn't exist".
    mockListResources.mockRejectedValue(
      new ApiError({ message: "backend offline", status: 500 }),
    );
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "backend offline", status: 500 }),
    );

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("backend offline");
    });
    // Fetch error renders the generic title, NOT the not-found title
    expect(screen.getByText("resources.detail.title")).toBeInTheDocument();
    expect(
      screen.queryByText("resources.detail.notFoundTitle"),
    ).not.toBeInTheDocument();
  });

  it("exposes all five tabs (overview / data / schemas / tokens / events)", async () => {
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockResolvedValue(makeSchema());

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("EC Products")).toBeInTheDocument();
    });
    for (const key of [
      "overview",
      "data",
      "schemas",
      "tokens",
      "events",
    ] as const) {
      // Tabs carry an icon next to the label; matching by accessible name
      // requires a regex because the label text is a substring of the rendered
      // tab content.
      expect(
        screen.getByRole("tab", {
          name: new RegExp(`resources\\.tabs\\.${key}`),
        }),
      ).toBeInTheDocument();
    }
  });

  it("passes useParams() id through unchanged (App Router already decodes)", async () => {
    // Simulate useParams()'s actual behavior: it returns the already-decoded
    // route segment. So the fixture uses the decoded form directly.
    mockParamsId = "has spaces";
    mockListResources.mockResolvedValue({
      resources: [makeResource({ resource_id: "has spaces" })],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "Not found", status: 404 }),
    );

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(mockGetSchema).toHaveBeenCalledWith("has spaces");
    });
  });

  it("does not throw URIError on resource_ids containing a literal %", async () => {
    // Regression: decodeURIComponent("%") throws URIError. Since useParams
    // already decodes, the page must not re-decode — only apply decode() once.
    mockParamsId = "50%-complete";
    mockListResources.mockResolvedValue({
      resources: [makeResource({ resource_id: "50%-complete" })],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(
      new ApiError({ message: "Not found", status: 404 }),
    );

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(mockGetSchema).toHaveBeenCalledWith("50%-complete");
    });
  });

  it("renders upgrade CTA and skips fetch on basic plan", async () => {
    mockCurrentWorkspace = { plan_name: "basic", current_user_role: "owner" };

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("resources.planGate.title")).toBeInTheDocument();
    });
    expect(mockListResources).not.toHaveBeenCalled();
    expect(mockGetSchema).not.toHaveBeenCalled();
  });

  it("holds the fetch until WorkspaceContext hydrates", async () => {
    mockCurrentWorkspace = null;
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockResolvedValue(makeSchema());

    render(<ResourceDetailPage />);

    await Promise.resolve();
    expect(mockListResources).not.toHaveBeenCalled();
  });

  // Issue #389: owner-only access on the detail page — same rule as the
  // list page, applied identically so a direct deep-link behaves the same.
  it.each([["admin"], ["member"], ["viewer"]])(
    "redirects to /workspace/dashboard for role=%s and does not fetch",
    async (role) => {
      mockCurrentWorkspace = { plan_name: "pro", current_user_role: role };

      render(<ResourceDetailPage />);

      await waitFor(() => {
        expect(mockRouterPush).toHaveBeenCalledWith("/workspace/dashboard");
      });
      expect(mockListResources).not.toHaveBeenCalled();
      expect(mockGetSchema).not.toHaveBeenCalled();
    },
  );
});
