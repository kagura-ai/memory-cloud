/**
 * SearchSettingsSection — the Purpose select (#2983).
 *
 * A select above the weights: conversation and work history, documents and
 * manuals, or custom. Choosing a purpose stages its weights and fusion on the
 * save bar (nothing is sent until Save); the select shows the purpose whose
 * values equal the current ones, else Custom. A backend that predates the
 * fusion setting matches and stages the weights only.
 *
 * The Radix select is swapped for a native one (jsdom has no pointer events).
 * Under the echo translator a message renders as `key(a=1,b=2)`.
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
import { SEARCH_PURPOSE_PRESETS } from "@/lib/searchPurposes";

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

/** A current backend: the stored fusion and what auto resolves to. */
const CURRENT = { ...BASE, fusion: "auto", fusion_effective: "rrf" };

/** The six fields the PUT schema requires, at their stored values. */
const STORED_FIELDS = {
  semantic_weight: 0.6,
  bm25_weight: 0.4,
  fetch_factor: 3,
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
};

/** The select whose options include `value`. */
function selectWith(value: string): HTMLSelectElement {
  const found = (screen.getAllByRole("combobox") as HTMLSelectElement[]).filter(
    (s) => Array.from(s.options).some((o) => o.value === value),
  );
  expect(found).toHaveLength(1);
  return found[0];
}

const purposeSelect = () => selectWith("documents");
const fusionSelect = () => selectWith("rrf");

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

describe("purpose presets — values", () => {
  it("conversation is today's default and documents leans on the semantic side", () => {
    expect(SEARCH_PURPOSE_PRESETS).toEqual({
      conversation: { semantic_weight: 0.6, bm25_weight: 0.4, fusion: "auto" },
      documents: { semantic_weight: 0.8, bm25_weight: 0.2, fusion: "auto" },
    });
  });
});

describe("purpose select — presence", () => {
  it("renders above the weights with the three purposes and their guidance", async () => {
    await renderWith(CURRENT);
    expect(screen.getByText("purpose.label")).toBeTruthy();
    const options = Array.from(purposeSelect().options).map((o) => o.value);
    expect(options).toEqual(["conversation", "documents", "custom"]);
    expect(
      screen.getByText("purpose.conversationGuidance(semantic=0.6,keyword=0.4)"),
    ).toBeTruthy();
    expect(
      screen.getByText("purpose.documentsGuidance(semantic=0.8,keyword=0.2)"),
    ).toBeTruthy();
    expect(screen.getByText("purpose.customGuidance")).toBeTruthy();
    // Above the weights in the document order.
    const purposeLabel = screen.getByText("purpose.label");
    const weightLabel = screen.getByText("semanticWeightLabel");
    expect(
      purposeLabel.compareDocumentPosition(weightLabel) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe("purpose select — matching the stored values", () => {
  it.each([
    ["conversation", { semantic_weight: 0.6, bm25_weight: 0.4, fusion: "auto" }],
    ["documents", { semantic_weight: 0.8, bm25_weight: 0.2, fusion: "auto" }],
    // Weights compare after rounding to two places.
    ["documents", { semantic_weight: 0.8000001, bm25_weight: 0.1999999, fusion: "auto" }],
    ["custom", { semantic_weight: 0.7, bm25_weight: 0.3, fusion: "auto" }],
    // A non-auto fusion is Custom even with a purpose's weights.
    ["custom", { semantic_weight: 0.6, bm25_weight: 0.4, fusion: "rrf" }],
    ["custom", { semantic_weight: 0.8, bm25_weight: 0.2, fusion: "weighted" }],
  ])("shows %s for %j", async (expected, values) => {
    await renderWith({ ...CURRENT, ...values });
    expect(purposeSelect().value).toBe(expected);
  });

  it("follows a staged weight edit", async () => {
    await renderWith(CURRENT);
    expect(purposeSelect().value).toBe("conversation");
    fireEvent.change(screen.getByLabelText("semanticWeightLabel"), {
      target: { value: "0.7" },
    });
    expect(purposeSelect().value).toBe("custom");
    fireEvent.change(screen.getByLabelText("semanticWeightLabel"), {
      target: { value: "0.8" },
    });
    expect(purposeSelect().value).toBe("documents");
  });

  it("follows a staged fusion edit", async () => {
    await renderWith(CURRENT);
    fireEvent.change(fusionSelect(), { target: { value: "rrf" } });
    expect(purposeSelect().value).toBe("custom");
  });
});

describe("purpose select — staging", () => {
  it("choosing documents stages 0.8 / 0.2 / auto, and Save sends them", async () => {
    await renderWith({ ...CURRENT, fusion: "rrf", fusion_effective: "rrf" });
    expect(purposeSelect().value).toBe("custom");
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });

    expect(purposeSelect().value).toBe("documents");
    expect(
      (screen.getByLabelText("semanticWeightLabel") as HTMLInputElement).value,
    ).toBe("0.8");
    expect(fusionSelect().value).toBe("auto");
    expect(saveBar()).not.toBeNull();
    expect(mockUpdateConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      semantic_weight: 0.8,
      bm25_weight: 0.2,
      fusion: "auto",
    });
  });

  it("an unchanged fusion stays out of the save", async () => {
    await renderWith(CURRENT);
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });
    fireEvent.click(await screen.findByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      semantic_weight: 0.8,
      bm25_weight: 0.2,
    });
  });

  it("Discard restores the stored values", async () => {
    await renderWith(CURRENT);
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });
    expect(saveBar()).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "discardChanges" }));
    await waitFor(() => expect(saveBar()).toBeNull());
    expect(purposeSelect().value).toBe("conversation");
    expect(
      (screen.getByLabelText("semanticWeightLabel") as HTMLInputElement).value,
    ).toBe("0.6");
    expect(mockUpdateConfig).not.toHaveBeenCalled();
  });

  it("choosing the stored purpose again clears the bar", async () => {
    await renderWith(CURRENT);
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });
    expect(saveBar()).not.toBeNull();
    fireEvent.change(purposeSelect(), { target: { value: "conversation" } });
    await waitFor(() => expect(saveBar()).toBeNull());
  });

  it("a staged purpose hides what auto resolves to now, and Discard brings it back", async () => {
    await renderWith(CURRENT);
    const current = "fusion.autoCurrent(effective=fusion.effectiveRrf)";
    expect(screen.getByText(current)).toBeTruthy();
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });
    // The stored resolution says nothing about the staged weights.
    expect(screen.queryByText(current)).toBeNull();
    expect(fusionSelect().value).toBe("auto");
    fireEvent.click(screen.getByRole("button", { name: "discardChanges" }));
    await waitFor(() => expect(screen.getByText(current)).toBeTruthy());
  });

  it("choosing Custom stages nothing", async () => {
    await renderWith({ ...CURRENT, semantic_weight: 0.7, bm25_weight: 0.3 });
    fireEvent.change(purposeSelect(), { target: { value: "custom" } });
    expect(saveBar()).toBeNull();
    expect(purposeSelect().value).toBe("custom");
  });
});

describe("purpose select — a backend that predates the fusion setting", () => {
  it("still shows, and matches on the weights only", async () => {
    await renderWith({ ...BASE, semantic_weight: 0.8, bm25_weight: 0.2 });
    expect(screen.queryByText("fusion.label")).toBeNull();
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
    expect(purposeSelect().value).toBe("documents");
  });

  it("choosing a purpose stages and sends the weights only", async () => {
    await renderWith(BASE);
    expect(purposeSelect().value).toBe("conversation");
    fireEvent.change(purposeSelect(), { target: { value: "documents" } });
    expect(purposeSelect().value).toBe("documents");
    fireEvent.click(await screen.findByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    const body = mockUpdateConfig.mock.calls[0][1];
    expect(body).toEqual({
      ...STORED_FIELDS,
      semantic_weight: 0.8,
      bm25_weight: 0.2,
    });
    expect(body).not.toHaveProperty("fusion");
  });
});
