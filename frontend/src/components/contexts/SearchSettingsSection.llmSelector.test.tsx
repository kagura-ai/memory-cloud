/**
 * SearchSettingsSection — the recall LLM selector card (#2148, #2144).
 *
 * Same shape as the content chunk card: the card reads only what GET
 * /search-config computes server-side — the stored flag, whether it is acted
 * on (`llm_selector_effective`), whether this caller may change it
 * (`llm_selector_can_change`) and why not (`llm_selector_disabled_reason`),
 * the plan cap and the workspace count. The tier matrix is deliberately NOT
 * consulted: the plan gate only applies where the deployment turns it on.
 *
 * Under the echo translator a message renders as `key(a=1,b=2)`.
 */

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
  useTranslations: (_ns?: string) => {
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

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

let mockPlan = "basic";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: { id: "ws-1", plan_name: mockPlan },
  }),
}));

let mockInfo: { features: Record<string, boolean> } | null = {
  features: { byok: false },
};
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockInfo?.features ?? null,
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

import { ApiError } from "@/lib/api/base";
import { SearchSettingsSection } from "./SearchSettingsSection";

/** A refusal as `lib/api/base.ts` hands it over: the gate facts attached. */
function refusal(gate: Record<string, unknown>) {
  // The mocked ApiError is a bare Error subclass taking a message.
  const MockApiError = ApiError as unknown as new (message: string) => Error;
  return Object.assign(new MockApiError("refused"), { gate });
}

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

/** A flag-off context the caller may turn on, on an ungated deployment. */
const SELECTOR_OFF = {
  ...BASE,
  llm_selector_enabled: false,
  llm_selector_effective: false,
  llm_selector_limit: null,
  llm_selector_in_use: 0,
  llm_selector_disabled_reason: null,
  llm_selector_can_change: true,
};

const SELECTOR_ON = {
  ...SELECTOR_OFF,
  llm_selector_enabled: true,
  llm_selector_effective: true,
  llm_selector_in_use: 1,
};

/** The six fields the PUT schema requires, at their stored values. */
const STORED_FIELDS = {
  semantic_weight: 0.6,
  bm25_weight: 0.4,
  fetch_factor: 3,
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
};

function saveBar() {
  return screen.queryByTestId("search-settings-save-bar");
}

function selectorSwitch() {
  return screen.getByRole("switch", { name: "llmSelector.toggleLabel" });
}

async function renderWith(config: Record<string, unknown>) {
  mockGetConfig.mockResolvedValue(config);
  render(<SearchSettingsSection contextId="ctx-1" />);
  await waitFor(() =>
    expect(screen.getByText("llmSelector.title")).toBeTruthy(),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockInfo = { features: { byok: false } };
  mockPlan = "basic";
  mockUpdateConfig.mockResolvedValue({});
});

afterEach(() => cleanup());

describe("LLM selector card — presence", () => {
  it("is not rendered by a backend that predates the fields", async () => {
    mockGetConfig.mockResolvedValue(BASE);
    render(<SearchSettingsSection contextId="ctx-1" />);
    await waitFor(() =>
      expect(screen.getByText("hybridSearchWeights")).toBeTruthy(),
    );
    expect(screen.queryByText("llmSelector.title")).toBeNull();
  });

  it("enabled: the stored state, active badge, and an enabled switch", async () => {
    await renderWith(SELECTOR_ON);
    expect(selectorSwitch().getAttribute("aria-checked")).toBe("true");
    expect(selectorSwitch().hasAttribute("disabled")).toBe(false);
    expect(screen.getByText("llmSelector.activeBadge")).toBeTruthy();
    expect(screen.queryByText("llmSelector.inactiveBadge")).toBeNull();
  });

  it("gate off: no in-use / limit line where the deployment does not gate by plan", async () => {
    await renderWith(SELECTOR_ON);
    expect(screen.queryByText(/llmSelector\.usage/)).toBeNull();
    expect(screen.queryByText(/llmSelector\.reason/)).toBeNull();
  });

  it("gated with a free slot: shows in-use of limit", async () => {
    await renderWith({
      ...SELECTOR_OFF,
      llm_selector_limit: 3,
      llm_selector_in_use: 1,
    });
    expect(screen.getByText("llmSelector.usage(inUse=1,limit=3)")).toBeTruthy();
    expect(selectorSwitch().hasAttribute("disabled")).toBe(false);
  });
});

describe("LLM selector card — enabling", () => {
  it("asks first, saying what is sent where and that results are cut, and saves on confirm", async () => {
    await renderWith(SELECTOR_OFF);
    mockUpdateConfig.mockResolvedValue(SELECTOR_ON);
    fireEvent.click(selectorSwitch());

    expect(screen.getByText("llmSelector.enableConfirmTitle")).toBeTruthy();
    expect(screen.getByText("llmSelector.enableConfirmBody")).toBeTruthy();
    expect(screen.getByText("llmSelector.enableConfirmPassages")).toBeTruthy();
    // Nothing is sent until confirmed.
    expect(mockUpdateConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("llmSelector.confirmEnable"));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    // #2159: one PUT, the stored required fields plus the flag; no bar.
    expect(mockUpdateConfig).toHaveBeenCalledTimes(1);
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      llm_selector_enabled: true,
    });
    await waitFor(() =>
      expect(screen.getByText("llmSelector.activeBadge")).toBeTruthy(),
    );
    expect(selectorSwitch().getAttribute("aria-checked")).toBe("true");
    expect(saveBar()).toBeNull();
  });

  it("keeps a staged numeric edit out of the toggle save, and staged", async () => {
    await renderWith(SELECTOR_OFF);
    fireEvent.change(screen.getByLabelText("semanticWeightLabel"), {
      target: { value: "0.8" },
    });
    mockUpdateConfig.mockResolvedValue(SELECTOR_ON);
    fireEvent.click(selectorSwitch());
    fireEvent.click(screen.getByText("llmSelector.confirmEnable"));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig).toHaveBeenCalledTimes(1);
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      llm_selector_enabled: true,
    });
    expect(
      screen.getByText("unsavedFields(fields=unsavedFieldSemanticWeight)"),
    ).toBeTruthy();
  });

  it("cancelling sends nothing", async () => {
    await renderWith(SELECTOR_OFF);
    fireEvent.click(selectorSwitch());
    fireEvent.click(screen.getByText("cancel"));
    await waitFor(() =>
      expect(screen.queryByText("llmSelector.enableConfirmTitle")).toBeNull(),
    );
    expect(selectorSwitch().getAttribute("aria-checked")).toBe("false");
    expect(mockUpdateConfig).not.toHaveBeenCalled();
    expect(saveBar()).toBeNull();
  });

  it("does not send the flag when only another setting changed", async () => {
    await renderWith(SELECTOR_OFF);
    fireEvent.change(screen.getByLabelText("fetchFactorLabel"), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByText("saveChanges"));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).not.toHaveProperty(
      "llm_selector_enabled",
    );
  });

  it.each([
    ["plan", { state: "plan", feature: "llm_selector" }, "plan"],
    ["cap", { state: "quota", quotaType: "llm_selector_contexts" }, "cap"],
    ["role", { state: "role" }, "permission"],
  ])(
    "a %s refusal toasts, re-reads the config and reverts the switch",
    async (_label, gate, reason) => {
      await renderWith(SELECTOR_OFF);
      mockUpdateConfig.mockRejectedValueOnce(refusal(gate));
      mockGetConfig.mockResolvedValue({
        ...SELECTOR_OFF,
        llm_selector_can_change: false,
        llm_selector_disabled_reason: reason,
      });
      fireEvent.click(selectorSwitch());
      fireEvent.click(screen.getByText("llmSelector.confirmEnable"));
      await waitFor(() =>
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            variant: "destructive",
            description: "refused",
          }),
        ),
      );
      await waitFor(() =>
        expect(selectorSwitch().hasAttribute("disabled")).toBe(true),
      );
      expect(selectorSwitch().getAttribute("aria-checked")).toBe("false");
      expect(saveBar()).toBeNull();
    },
  );

  it("any other failure toasts and reverts the switch", async () => {
    await renderWith(SELECTOR_OFF);
    mockUpdateConfig.mockRejectedValueOnce(new Error("network down"));
    fireEvent.click(selectorSwitch());
    fireEvent.click(screen.getByText("llmSelector.confirmEnable"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          description: "network down",
        }),
      ),
    );
    await waitFor(() =>
      expect(selectorSwitch().getAttribute("aria-checked")).toBe("false"),
    );
    expect(saveBar()).toBeNull();
  });
});

describe("LLM selector card — cap without counts", () => {
  it("says the cap without numbers when the server reports no limit", async () => {
    await renderWith({
      ...SELECTOR_OFF,
      llm_selector_limit: null,
      llm_selector_can_change: false,
      llm_selector_disabled_reason: "cap",
    });
    expect(screen.getByText("llmSelector.reasonCapNoCount")).toBeTruthy();
    expect(screen.queryByText(/llmSelector\.reasonCap\(/)).toBeNull();
  });

  it("the inactive note too", async () => {
    await renderWith({
      ...SELECTOR_ON,
      llm_selector_effective: false,
      llm_selector_limit: null,
      llm_selector_disabled_reason: "cap",
    });
    expect(screen.getByText("llmSelector.inactiveCapNoCount")).toBeTruthy();
  });
});

describe("LLM selector card — disabling", () => {
  it("saves at once without a dialog and sends false", async () => {
    await renderWith(SELECTOR_ON);
    mockUpdateConfig.mockResolvedValue(SELECTOR_OFF);
    fireEvent.click(selectorSwitch());
    expect(screen.queryByText("llmSelector.enableConfirmTitle")).toBeNull();
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      ...STORED_FIELDS,
      llm_selector_enabled: false,
    });
    await waitFor(() =>
      expect(selectorSwitch().getAttribute("aria-checked")).toBe("false"),
    );
    expect(saveBar()).toBeNull();
  });
});

describe("LLM selector card — disabled with the reason", () => {
  it.each([
    [
      "plan (disabled by plan)",
      "plan",
      { llm_selector_limit: 0 },
      "llmSelector.reasonPlan",
    ],
    [
      "cap (cap reached)",
      "cap",
      { llm_selector_limit: 2, llm_selector_in_use: 2 },
      "llmSelector.reasonCap(inUse=2,limit=2)",
    ],
    [
      "permission (not owner)",
      "permission",
      {},
      "llmSelector.reasonPermission",
    ],
  ] as const)("%s", async (_label, reason, extra, text) => {
    await renderWith({
      ...SELECTOR_OFF,
      ...extra,
      llm_selector_can_change: false,
      llm_selector_disabled_reason: reason,
    });
    const sw = selectorSwitch();
    expect(sw.hasAttribute("disabled")).toBe(true);
    const note = screen.getByText(text);
    // The reason is announced with the switch, not left to a tooltip.
    expect(sw.getAttribute("aria-describedby")).toContain(note.id);
    expect(note.id).not.toBe("");
    // The cap reason carries the numbers; no duplicate usage line.
    expect(screen.queryByText(/llmSelector\.usage/)).toBeNull();
  });

  it("not owner of a context that is on but over the cap: the note says cap, the switch says role", async () => {
    await renderWith({
      ...SELECTOR_ON,
      llm_selector_effective: false,
      llm_selector_limit: 1,
      llm_selector_in_use: 2,
      llm_selector_can_change: false,
      llm_selector_disabled_reason: "cap",
    });
    expect(selectorSwitch().hasAttribute("disabled")).toBe(true);
    expect(
      screen.getByText("llmSelector.inactiveCap(inUse=2,limit=1)"),
    ).toBeTruthy();
    expect(screen.getByText("llmSelector.reasonPermission")).toBeTruthy();
  });
});

describe("LLM selector card — stored on but inactive", () => {
  it.each(["plan", "cap"] as const)(
    "%s: badge, the note, and the switch still turns it off",
    async (reason) => {
      await renderWith({
        ...SELECTOR_ON,
        llm_selector_effective: false,
        llm_selector_limit: reason === "cap" ? 1 : 0,
        llm_selector_in_use: 2,
        llm_selector_disabled_reason: reason,
      });
      expect(screen.getByText("llmSelector.inactiveBadge")).toBeTruthy();
      expect(
        screen.getByText(
          reason === "plan"
            ? "llmSelector.inactivePlan"
            : "llmSelector.inactiveCap(inUse=2,limit=1)",
        ),
      ).toBeTruthy();
      expect(screen.getByText("llmSelector.inactiveNote")).toBeTruthy();
      expect(selectorSwitch().getAttribute("aria-checked")).toBe("true");
      expect(selectorSwitch().hasAttribute("disabled")).toBe(false);
    },
  );
});
