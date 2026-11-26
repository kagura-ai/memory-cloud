/**
 * SearchSettingsSection — the user facts lane switch (#3014).
 *
 * A switch in the weights card with a one-sentence helper. A staged change
 * rides the save bar and is sent only when it differs from the stored value;
 * a backend that predates the field omits it, and the switch is then not shown
 * and nothing is sent. The search purposes neither set it nor follow it.
 *
 * The Radix select is swapped for a native one (jsdom has no pointer events).
 */

import type { ReactNode } from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";
import type { PlanTierFeature } from "@/lib/api/workspaces";

vi.mock("next-intl", () => ({
  useTranslations: () => {
    const t = ((k: string, values?: Record<string, unknown>) =>
      values
        ? `${k}(${Object.entries(values)
            .map(([name, v]) => `${name}=${String(v)}`)
            .join(",")})`
        : k) as ((k: string, values?: Record<string, unknown>) => string) & {
      rich: (
        k: string,
        values?: Record<string, (chunks: string) => unknown>,
      ) => unknown;
    };
    t.rich = (k, values) => (values?.link ? values.link(k) : k);
    return t;
  },
  useFormatter: () => ({
    list: (items: Iterable<string>) => Array.from(items).join(", "),
  }),
  useLocale: () => "en",
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    disabled,
    children,
  }: {
    value: string;
    onValueChange?: (v: string) => void;
    disabled?: boolean;
    children: ReactNode;
  }) => (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
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

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: { id: "ws-1", plan_name: "basic" },
  }),
}));

const mockInfo = { features: { byok: false } };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockInfo.features,
  useSystemInfo: () => mockInfo,
}));

const TIERS = [
  { name: "free", display_name: "S", reranking: false },
  { name: "basic", display_name: "M", reranking: true },
] as unknown as PlanTierFeature[];
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => TIERS,
}));

const mockGetConfig = vi.fn();
const mockUpdateConfig = vi.fn();
vi.mock("@/lib/api/contexts", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    getContextSearchConfig: (...a: unknown[]) => mockGetConfig(...a),
    updateContextSearchConfig: (...a: unknown[]) => mockUpdateConfig(...a),
  };
});

vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/api/base", () => ({
  apiClient: { get: vi.fn().mockResolvedValue({ services: {} }) },
  ApiError: class ApiError extends Error {},
}));

import { SearchSettingsSection } from "./SearchSettingsSection";

const BASE = {
  context_id: "ctx-1",
  semantic_weight: 0.6,
  bm25_weight: 0.4,
  fetch_factor: 3,
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const ON = {
  ...BASE,
  fusion: "auto",
  fusion_effective: "rrf",
  user_facts_lane: true,
};
const OFF = { ...ON, user_facts_lane: false };

const STORED_FIELDS = {
  semantic_weight: 0.6,
  bm25_weight: 0.4,
  fetch_factor: 3,
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
};

function laneSwitch(): HTMLElement {
  return screen.getByRole("switch", { name: "userFactsLane.label" });
}

function purposeSelect(): HTMLSelectElement {
  const found = (
    screen.queryAllByRole("combobox") as HTMLSelectElement[]
  ).filter((s) =>
    Array.from(s.options).some((o) => o.value === "conversation"),
  );
  expect(found).toHaveLength(1);
  return found[0];
}

function saveBar() {
  return screen.queryByTestId("search-settings-save-bar");
}

async function renderWith(config: Record<string, unknown>) {
  mockGetConfig.mockResolvedValue(config);
  render(<SearchSettingsSection contextId="ctx-1" />);
  await waitFor(() =>
    expect(screen.getByText("hybridSearchWeights")).toBeTruthy(),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUpdateConfig.mockResolvedValue({});
});

afterEach(() => cleanup());

describe("user facts lane switch", () => {
  it("shows the stored value with its helper", async () => {
    await renderWith(ON);
    expect(laneSwitch().getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("userFactsLane.description")).toBeTruthy();
  });

  it("is off when stored off", async () => {
    await renderWith(OFF);
    expect(laneSwitch().getAttribute("aria-checked")).toBe("false");
  });

  it("is not shown by a backend that predates the field", async () => {
    await renderWith({ ...BASE, fusion: "auto", fusion_effective: "rrf" });
    expect(screen.queryByText("userFactsLane.label")).toBeNull();
    expect(
      screen.queryByRole("switch", { name: "userFactsLane.label" }),
    ).toBeNull();
  });

  it("stages a change on the save bar and sends it with the staged fields", async () => {
    await renderWith(OFF);
    fireEvent.click(laneSwitch());
    expect(
      await screen.findByText("unsavedFields(fields=unsavedFieldUserFacts)"),
    ).toBeTruthy();
    expect(mockUpdateConfig).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      user_facts_lane: true,
    });
  });

  it("switching back to the stored value clears the bar", async () => {
    await renderWith(ON);
    fireEvent.click(laneSwitch());
    expect(saveBar()).not.toBeNull();
    fireEvent.click(laneSwitch());
    await waitFor(() => expect(saveBar()).toBeNull());
  });

  it("an unchanged switch stays out of a save of other fields", async () => {
    await renderWith(ON);
    fireEvent.change(screen.getByLabelText("fetchFactorLabel"), {
      target: { value: "5" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).not.toHaveProperty(
      "user_facts_lane",
    );
  });

  it("does not change which purpose is shown", async () => {
    await renderWith(OFF);
    expect(purposeSelect().value).toBe("conversation");
    fireEvent.click(laneSwitch());
    expect(purposeSelect().value).toBe("conversation");
  });

  it("a purpose does not set the switch", async () => {
    await renderWith(OFF);
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });
    expect(laneSwitch().getAttribute("aria-checked")).toBe("false");
    fireEvent.click(await screen.findByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).not.toHaveProperty(
      "user_facts_lane",
    );
  });
});
