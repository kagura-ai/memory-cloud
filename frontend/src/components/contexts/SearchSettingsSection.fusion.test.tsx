/**
 * SearchSettingsSection — the context's hybrid fusion (#2980).
 *
 * A select in the weights card: auto / rrf / weighted, with the guidance on
 * which content each suits. For `auto` the label says what it resolves to now
 * (`fusion_effective`, server-computed). A staged change rides the save bar
 * and is sent only when it differs from the stored value; a backend that
 * predates the field omits it, and the control is then not shown and nothing
 * is sent.
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

const AUTO = { ...BASE, fusion: "auto", fusion_effective: "rrf" };

/** The six fields the PUT schema requires, at their stored values. */
const STORED_FIELDS = {
  semantic_weight: 0.6,
  bm25_weight: 0.4,
  fetch_factor: 3,
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
};

/** The fusion selects (#2983 added the purpose select beside it). */
function fusionSelects(): HTMLSelectElement[] {
  return (screen.queryAllByRole("combobox") as HTMLSelectElement[]).filter(
    (s) => Array.from(s.options).some((o) => o.value === "rrf"),
  );
}

function fusionSelect(): HTMLSelectElement {
  const found = fusionSelects();
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

describe("fusion select — presence", () => {
  it("shows the stored choice with the three options and the guidance", async () => {
    await renderWith({
      ...BASE,
      fusion: "weighted",
      fusion_effective: "weighted",
    });
    expect(screen.getByText("fusion.label")).toBeTruthy();
    expect(fusionSelect().value).toBe("weighted");
    const options = Array.from(fusionSelect().options).map((o) => o.value);
    expect(options).toEqual(["auto", "rrf", "weighted"]);
    expect(screen.getByText("fusion.description")).toBeTruthy();
    expect(screen.getByText("fusion.guidance")).toBeTruthy();
    expect(screen.getByText("fusion.autoDesc")).toBeTruthy();
  });

  it("names what auto resolves to now", async () => {
    await renderWith(AUTO);
    expect(fusionSelect().value).toBe("auto");
    expect(
      screen.getByText("fusion.autoCurrent(effective=fusion.effectiveRrf)"),
    ).toBeTruthy();
  });

  it("drops the effective value while a weight edit is staged, and brings it back on discard", async () => {
    await renderWith(AUTO);
    const current = "fusion.autoCurrent(effective=fusion.effectiveRrf)";
    expect(screen.getByText(current)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("semanticWeightLabel"), {
      target: { value: "0.8" },
    });
    // The stored effective value says nothing about the staged weights.
    expect(screen.queryByText(current)).toBeNull();
    expect(screen.getByText("fusion.auto")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "discardChanges" }));
    await waitFor(() => expect(screen.getByText(current)).toBeTruthy());
  });

  it("drops the effective value while the fusion select has a staged edit", async () => {
    await renderWith(AUTO);
    fireEvent.change(fusionSelect(), { target: { value: "weighted" } });
    expect(
      screen.queryByText("fusion.autoCurrent(effective=fusion.effectiveRrf)"),
    ).toBeNull();
    expect(screen.getByText("fusion.auto")).toBeTruthy();
  });

  it("names the keyword floor when auto resolves to it", async () => {
    await renderWith({ ...AUTO, fusion_effective: "keyword_floor" });
    expect(
      screen.getByText(
        "fusion.autoCurrent(effective=fusion.effectiveKeywordFloor)",
      ),
    ).toBeTruthy();
  });

  it("an explicit choice does not claim what auto would give", async () => {
    await renderWith({ ...BASE, fusion: "rrf", fusion_effective: "rrf" });
    expect(screen.queryByText(/fusion\.autoCurrent\(/)).toBeNull();
    expect(screen.getByText("fusion.auto")).toBeTruthy();
  });

  it("is not shown by a backend that predates the field", async () => {
    await renderWith(BASE);
    expect(screen.queryByText("fusion.label")).toBeNull();
    expect(fusionSelects()).toHaveLength(0);
  });
});

describe("fusion select — saving", () => {
  it("stages a change on the save bar and sends it with the staged fields", async () => {
    await renderWith(AUTO);
    fireEvent.change(fusionSelect(), { target: { value: "rrf" } });
    expect(
      await screen.findByText("unsavedFields(fields=unsavedFieldFusion)"),
    ).toBeTruthy();
    expect(saveBar()).not.toBeNull();
    expect(mockUpdateConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      fusion: "rrf",
    });
  });

  it("choosing the stored value again clears the bar", async () => {
    await renderWith(AUTO);
    fireEvent.change(fusionSelect(), { target: { value: "weighted" } });
    expect(saveBar()).not.toBeNull();
    fireEvent.change(fusionSelect(), { target: { value: "auto" } });
    await waitFor(() => expect(saveBar()).toBeNull());
  });

  it("an unchanged fusion stays out of a save of other fields", async () => {
    await renderWith(AUTO);
    fireEvent.change(screen.getByLabelText("fetchFactorLabel"), {
      target: { value: "5" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      fetch_factor: 5,
    });
  });

  it("an older backend: a save sends no fusion", async () => {
    await renderWith(BASE);
    fireEvent.change(screen.getByLabelText("fetchFactorLabel"), {
      target: { value: "4" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "saveChanges" }));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).not.toHaveProperty("fusion");
  });
});
