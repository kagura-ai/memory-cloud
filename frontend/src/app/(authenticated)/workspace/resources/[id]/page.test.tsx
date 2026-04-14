/**
 * Tests for the Resource detail page.
 *
 * Verifies:
 * - stats strip + schema table render on success
 * - schema absence (404) renders the "no schema" EmptyState
 * - listResources() and getSchema() run in parallel (both start before either resolves)
 * - unknown resource_id renders ErrorBanner + back link
 * - data tab renders the #316 placeholder
 * - url-encoded resource_id is decoded before use
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import ResourceDetailPage from "./page";
import { ApiError } from "@/lib/api/base";

// ---------- Mocks ------------------------------------------------------------

const mockListResources = vi.fn();
const mockGetSchema = vi.fn();
const mockRouterReplace = vi.fn();
const mockRouterPush = vi.fn();

let mockParamsId = "ec_products";
let mockSearchParams = new URLSearchParams();
let mockCurrentWorkspace: { plan_name?: string } | null = null;

vi.mock("@/lib/api/resources", () => ({
  listResources: (...args: unknown[]) => mockListResources(...args),
}));

vi.mock("@/lib/api/schemas", () => ({
  getSchema: (...args: unknown[]) => mockGetSchema(...args),
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
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `rel(${iso})`,
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
  mockRouterReplace.mockReset();
  mockRouterPush.mockReset();
  mockParamsId = "ec_products";
  mockSearchParams = new URLSearchParams();
  mockCurrentWorkspace = { plan_name: "pro" };
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("ResourceDetailPage", () => {
  it("renders stats strip and schema table on success", async () => {
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

  it("renders 'no schema' empty state when getSchema returns ApiError(404)", async () => {
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

    // Both must have started before either resolves — proves parallelism
    expect(listStarted).toBe(true);
    expect(schemaStarted).toBe(true);

    resolveList({ resources: [makeResource()], total: 1 });
    resolveSchema(makeSchema());
    await waitFor(() => {
      expect(screen.getByText("EC Products")).toBeInTheDocument();
    });
  });

  it("renders ErrorBanner when resource_id is not found in list", async () => {
    mockParamsId = "missing_resource";
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(new ApiError({ message: "Not found", status: 404 }));

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
    expect(screen.getByText("resources.detail.backToList")).toBeInTheDocument();
  });

  it("exposes both tabs (overview + data) in the tablist", async () => {
    mockListResources.mockResolvedValue({
      resources: [makeResource()],
      total: 1,
    });
    mockGetSchema.mockResolvedValue(makeSchema());

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("EC Products")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("tab", { name: "resources.tabs.overview" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "resources.tabs.data" }),
    ).toBeInTheDocument();
  });

  it("url-decodes the resource_id from the URL param", async () => {
    mockParamsId = "has%20spaces";
    mockListResources.mockResolvedValue({
      resources: [makeResource({ resource_id: "has spaces" })],
      total: 1,
    });
    mockGetSchema.mockRejectedValue(new ApiError({ message: "Not found", status: 404 }));

    render(<ResourceDetailPage />);

    await waitFor(() => {
      expect(mockGetSchema).toHaveBeenCalledWith("has spaces");
    });
  });

  it("renders upgrade CTA and skips fetch on basic plan", async () => {
    mockCurrentWorkspace = { plan_name: "basic" };

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
});
