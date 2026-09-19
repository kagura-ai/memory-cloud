/**
 * Tests for the admin Environment page (#1580).
 *
 * Every key the backend serves is env-backed, so the page is a read-only
 * console: it must show the value the API reports (the effective one) and
 * offer NO editable control and no Save — a saved-but-ineffective value is
 * worse than a read-only one.
 */

import { render, screen, within, cleanup } from "@testing-library/react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import EnvironmentPage from "./page";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPut = vi.fn();

vi.mock("@/lib/api", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    put: (...args: unknown[]) => mockPut(...args),
  },
}));

// Namespace-aware translator so assertions use fully-qualified keys.
vi.mock("next-intl", () => ({
  useTranslations:
    (namespace: string) =>
    (key: string, values?: Record<string, string | number>) => {
      const fullKey = namespace ? `${namespace}.${key}` : key;
      return values && Object.keys(values).length > 0
        ? `${fullKey}:${JSON.stringify(values)}`
        : fullKey;
    },
}));

vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

const item = (key: string, value: unknown, category: string) => ({
  key,
  value,
  category,
  description: null,
  is_sensitive: false,
  read_only: true,
});

const CONFIGS = [
  item("EMBEDDING_DIMENSIONS", 512, "embedding"),
  item("ENABLE_RERANKING", false, "search"),
  item("LOG_LEVEL", "INFO", "system"),
  item("CORS_ORIGINS", "http://localhost:3000", "system"),
  item("ENABLE_BYOK", true, "hosted"),
  item("MANAGED_LLM_PROVIDER", "", "hosted"),
  item("RERANK_BASE_URL", "https://***@reranker:8443/v1", "hosted"),
];

const SCHEMA = {
  EMBEDDING_DIMENSIONS: {
    type: "number",
    description: "Embedding vector dimensions",
    min_value: 128,
    max_value: 3072,
    requires_restart: true,
  },
  ENABLE_RERANKING: {
    type: "boolean",
    description: "Enable AI reranking",
    requires_restart: true,
  },
  LOG_LEVEL: {
    type: "enum",
    description: "Logging verbosity level",
    enum_values: ["DEBUG", "INFO"],
    enum_descriptions: { INFO: "General informational messages" },
    requires_restart: true,
  },
  ENABLE_BYOK: {
    type: "boolean",
    description: "Enable BYOK provisioning",
    requires_restart: true,
  },
  MANAGED_LLM_PROVIDER: {
    type: "string",
    description: "Provider of the managed LLM lane",
    requires_restart: true,
  },
  RERANK_BASE_URL: {
    type: "string",
    description: "Rerank endpoint",
    requires_restart: true,
  },
};

function mockApi(configs = CONFIGS) {
  mockGet.mockImplementation((url: string) => {
    if (url.startsWith("/api/v1/config/schema")) return Promise.resolve(SCHEMA);
    if (url.startsWith("/api/v1/config"))
      return Promise.resolve({ configs, total: configs.length });
    // telemetry / embedding models: the page tolerates their absence
    return Promise.reject(new Error("unavailable"));
  });
}

/** The bordered block that holds one key. */
const row = (key: string) =>
  screen.getByText(key).closest("[data-config-key]") as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  mockApi();
});

afterEach(() => cleanup());

describe("EnvironmentPage (read-only console)", () => {
  it("offers no editable control and no Save", async () => {
    render(<EnvironmentPage />);
    await screen.findByText("ENABLE_RERANKING");

    expect(screen.queryAllByRole("switch")).toHaveLength(0);
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(screen.queryAllByRole("spinbutton")).toHaveLength(0);
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);

    // Refresh is the only action left.
    expect(
      screen.getAllByRole("button").map((button) => button.textContent),
    ).toEqual(["admin.environment.actions.refresh"]);
  });

  it("shows the value the API reports, as static text", async () => {
    render(<EnvironmentPage />);
    await screen.findByText("ENABLE_RERANKING");

    expect(
      within(row("ENABLE_RERANKING")).getByText(
        "admin.environment.messages.disabled",
      ),
    ).toBeInTheDocument();
    expect(
      within(row("ENABLE_BYOK")).getByText(
        "admin.environment.messages.enabled",
      ),
    ).toBeInTheDocument();
    expect(within(row("EMBEDDING_DIMENSIONS")).getByText("512")).toBeVisible();
    expect(within(row("LOG_LEVEL")).getAllByText("INFO")[0]).toBeVisible();
    expect(
      within(row("CORS_ORIGINS")).getByText("http://localhost:3000"),
    ).toBeVisible();
    // The backend masks embedded credentials; the page renders it verbatim.
    expect(
      within(row("RERANK_BASE_URL")).getByText("https://***@reranker:8443/v1"),
    ).toBeVisible();
  });

  it("renders an unset string as 'not set' rather than a blank", async () => {
    render(<EnvironmentPage />);
    await screen.findByText("MANAGED_LLM_PROVIDER");

    expect(
      within(row("MANAGED_LLM_PROVIDER")).getByText(
        "admin.environment.messages.notSet",
      ),
    ).toBeInTheDocument();
  });

  it("explains that values come from the environment and marks keys read-only", async () => {
    render(<EnvironmentPage />);
    await screen.findByText("ENABLE_RERANKING");

    expect(
      screen.getByText("admin.environment.readOnlyNotice"),
    ).toBeInTheDocument();
    for (const { key } of CONFIGS) {
      expect(
        within(row(key)).getByText("admin.environment.actions.readOnly"),
      ).toBeInTheDocument();
    }
  });

  it("shows the hosted-mode category under its own title", async () => {
    render(<EnvironmentPage />);

    expect(
      await screen.findByText("admin.environment.sections.hosted"),
    ).toBeInTheDocument();
  });

  it("only ever reads from the config API", async () => {
    render(<EnvironmentPage />);
    await screen.findByText("ENABLE_RERANKING");

    expect(mockGet).toHaveBeenCalledWith("/api/v1/config?mask_sensitive=true");
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
  });
});
