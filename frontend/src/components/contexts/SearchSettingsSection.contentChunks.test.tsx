/**
 * SearchSettingsSection — the content chunk vectors card (#2104, #2015 PR 2c).
 *
 * The card reads only what GET /search-config computes server-side: the
 * stored flag, whether it is acted on (`content_chunks_effective`), whether
 * this caller may turn it on (`content_chunks_can_change`) and why not
 * (`content_chunks_disabled_reason`), the plan cap and the workspace count,
 * and the backfill queue. The tier matrix is deliberately NOT consulted: the
 * plan gate only applies where the deployment turns it on.
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
const CHUNKS_OFF = {
  ...BASE,
  content_chunks_enabled: false,
  content_chunks_can_change: true,
  content_chunks_supported: true,
  embedding_provider: "openai",
  content_chunks_backfill_pending: 0,
  content_chunks_effective: false,
  content_chunks_limit: null,
  content_chunks_in_use: 0,
  content_chunks_disabled_reason: null,
};

const CHUNKS_ON = {
  ...CHUNKS_OFF,
  content_chunks_enabled: true,
  content_chunks_effective: true,
  content_chunks_in_use: 1,
};

function chunkSwitch() {
  return screen.getByRole("switch", { name: "contentChunks.toggleLabel" });
}

async function renderWith(config: Record<string, unknown>) {
  mockGetConfig.mockResolvedValue(config);
  render(<SearchSettingsSection contextId="ctx-1" />);
  await waitFor(() =>
    expect(screen.getByText("contentChunks.title")).toBeTruthy(),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockInfo = { features: { byok: false } };
  mockPlan = "basic";
  mockUpdateConfig.mockResolvedValue({});
});

afterEach(() => cleanup());

describe("content chunk card — presence", () => {
  it("is not rendered by a backend that predates the fields", async () => {
    mockGetConfig.mockResolvedValue(BASE);
    render(<SearchSettingsSection contextId="ctx-1" />);
    await waitFor(() =>
      expect(screen.getByText("hybridSearchWeights")).toBeTruthy(),
    );
    expect(screen.queryByText("contentChunks.title")).toBeNull();
  });

  it("shows the stored state with an enabled switch", async () => {
    await renderWith(CHUNKS_ON);
    expect(chunkSwitch().getAttribute("aria-checked")).toBe("true");
    expect(chunkSwitch().hasAttribute("disabled")).toBe(false);
    expect(screen.queryByText("contentChunks.inactiveBadge")).toBeNull();
  });
});

describe("content chunk card — enabling", () => {
  it("asks first, naming the embedding provider, and stages the change", async () => {
    await renderWith(CHUNKS_OFF);
    fireEvent.click(chunkSwitch());

    expect(screen.getByText("contentChunks.enableConfirmTitle")).toBeTruthy();
    expect(
      screen.getByText("contentChunks.enableConfirmBody(provider=openai)"),
    ).toBeTruthy();
    expect(
      screen.getByText("contentChunks.enableConfirmBackfill"),
    ).toBeTruthy();
    // Reranking is off on this context: no reranker sentence.
    expect(screen.queryByText("contentChunks.enableConfirmRerank")).toBeNull();
    // Nothing is staged until confirmed.
    expect(screen.queryByText("unsavedChangesBar")).toBeNull();

    fireEvent.click(screen.getByText("contentChunks.confirmEnable"));
    await waitFor(() =>
      expect(screen.getByText("unsavedChangesBar")).toBeTruthy(),
    );
    expect(chunkSwitch().getAttribute("aria-checked")).toBe("true");

    fireEvent.click(screen.getByText("saveChanges"));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toMatchObject({
      content_chunks_enabled: true,
    });
  });

  it("adds the reranker sentence when reranking is on", async () => {
    await renderWith({ ...CHUNKS_OFF, use_rerank: true });
    fireEvent.click(chunkSwitch());
    expect(screen.getByText("contentChunks.enableConfirmRerank")).toBeTruthy();
  });

  it("omits it where the plan does not rerank, whatever is stored", async () => {
    mockPlan = "free";
    await renderWith({ ...CHUNKS_OFF, use_rerank: true });
    fireEvent.click(chunkSwitch());
    expect(screen.getByText("contentChunks.enableConfirmTitle")).toBeTruthy();
    expect(screen.queryByText("contentChunks.enableConfirmRerank")).toBeNull();
  });

  it("falls back to the provider-less copy when the provider is unknown", async () => {
    await renderWith({ ...CHUNKS_OFF, embedding_provider: null });
    fireEvent.click(chunkSwitch());
    expect(
      screen.getByText("contentChunks.enableConfirmBodyNoProvider"),
    ).toBeTruthy();
  });

  it("cancelling stages nothing", async () => {
    await renderWith(CHUNKS_OFF);
    fireEvent.click(chunkSwitch());
    fireEvent.click(screen.getByText("cancel"));
    await waitFor(() =>
      expect(screen.queryByText("contentChunks.enableConfirmTitle")).toBeNull(),
    );
    expect(chunkSwitch().getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByText("unsavedChangesBar")).toBeNull();
  });

  it("does not send the flag when only another setting changed", async () => {
    await renderWith(CHUNKS_OFF);
    fireEvent.change(screen.getByLabelText("fetchFactorLabel"), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByText("saveChanges"));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).not.toHaveProperty(
      "content_chunks_enabled",
    );
  });

  it.each([
    ["plan", { state: "plan", feature: "content_chunks" }],
    ["cap", { state: "quota", quotaType: "content_chunk_contexts" }],
  ])(
    "a %s refusal re-reads the config and drops only the chunk change",
    async (_label, gate) => {
      await renderWith(CHUNKS_OFF);
      mockUpdateConfig.mockRejectedValueOnce(refusal(gate));
      fireEvent.click(chunkSwitch());
      fireEvent.click(screen.getByText("contentChunks.confirmEnable"));
      fireEvent.change(screen.getByLabelText("fetchFactorLabel"), {
        target: { value: "5" },
      });
      fireEvent.click(await screen.findByText("saveChanges"));
      await waitFor(() =>
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            variant: "destructive",
            description: "refused",
          }),
        ),
      );
      await waitFor(() => expect(mockGetConfig).toHaveBeenCalledTimes(2));
      await waitFor(() =>
        expect(chunkSwitch().getAttribute("aria-checked")).toBe("false"),
      );
      // The other unsaved edit is still staged.
      expect(screen.getByText("unsavedChangesBar")).toBeTruthy();
    },
  );

  it("any other failure keeps the confirmed chunk change staged", async () => {
    await renderWith(CHUNKS_OFF);
    mockUpdateConfig.mockRejectedValueOnce(new Error("network down"));
    fireEvent.click(chunkSwitch());
    fireEvent.click(screen.getByText("contentChunks.confirmEnable"));
    fireEvent.click(await screen.findByText("saveChanges"));
    await waitFor(() => expect(mockToast).toHaveBeenCalled());
    expect(mockGetConfig).toHaveBeenCalledTimes(1);
    expect(chunkSwitch().getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("unsavedChangesBar")).toBeTruthy();
  });

  it("disables the switch on a store without chunks, even where the backend predates the reason", async () => {
    await renderWith({
      ...CHUNKS_OFF,
      content_chunks_supported: false,
      // A pre-#2103 backend: can_change is the owner rule alone.
      content_chunks_can_change: true,
      content_chunks_disabled_reason: undefined,
    });
    expect(chunkSwitch().hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("contentChunks.reasonUnsupported")).toBeTruthy();
  });
});

describe("content chunk card — disabling", () => {
  it("asks first, saying the chunk vectors are removed on save", async () => {
    await renderWith(CHUNKS_ON);
    fireEvent.click(chunkSwitch());
    expect(screen.getByText("contentChunks.disableConfirmTitle")).toBeTruthy();
    expect(screen.getByText("contentChunks.disableConfirmBody")).toBeTruthy();
    fireEvent.click(screen.getByText("contentChunks.confirmDisable"));
    fireEvent.click(await screen.findByText("saveChanges"));
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).toMatchObject({
      content_chunks_enabled: false,
    });
  });

  it("flipping back to the stored value needs no dialog and is not a change", async () => {
    await renderWith(CHUNKS_ON);
    fireEvent.click(chunkSwitch());
    fireEvent.click(screen.getByText("contentChunks.confirmDisable"));
    await waitFor(() =>
      expect(screen.getByText("unsavedChangesBar")).toBeTruthy(),
    );
    fireEvent.click(chunkSwitch());
    expect(screen.queryByText("contentChunks.enableConfirmTitle")).toBeNull();
    expect(chunkSwitch().getAttribute("aria-checked")).toBe("true");
    expect(screen.queryByText("unsavedChangesBar")).toBeNull();
  });
});

describe("content chunk card — disabled with the reason", () => {
  it.each([
    ["plan", {}, "contentChunks.reasonPlan"],
    [
      "cap",
      { content_chunks_limit: 1, content_chunks_in_use: 1 },
      "contentChunks.reasonCap(inUse=1,limit=1)",
    ],
    ["permission", {}, "contentChunks.reasonPermission"],
    [
      "unsupported",
      { content_chunks_supported: false },
      "contentChunks.reasonUnsupported",
    ],
  ] as const)("%s", async (reason, extra, text) => {
    await renderWith({
      ...CHUNKS_OFF,
      ...extra,
      content_chunks_can_change: false,
      content_chunks_disabled_reason: reason,
    });
    const sw = chunkSwitch();
    expect(sw.hasAttribute("disabled")).toBe(true);
    const note = screen.getByText(text);
    // The reason is announced with the switch, not left to a tooltip.
    expect(sw.getAttribute("aria-describedby")).toContain(note.id);
    expect(note.id).not.toBe("");
  });
});

describe("content chunk card — stored on but inactive", () => {
  it.each(["plan", "cap"] as const)(
    "%s: badge, the note, and the switch still turns it off",
    async (reason) => {
      await renderWith({
        ...CHUNKS_ON,
        content_chunks_effective: false,
        content_chunks_limit: reason === "cap" ? 1 : 0,
        content_chunks_in_use: 2,
        content_chunks_disabled_reason: reason,
      });
      expect(screen.getByText("contentChunks.inactiveBadge")).toBeTruthy();
      expect(
        screen.getByText(
          reason === "plan"
            ? "contentChunks.inactivePlan"
            : "contentChunks.inactiveCap(inUse=2,limit=1)",
        ),
      ).toBeTruthy();
      expect(screen.getByText("contentChunks.inactiveKept")).toBeTruthy();
      expect(chunkSwitch().getAttribute("aria-checked")).toBe("true");
      expect(chunkSwitch().hasAttribute("disabled")).toBe(false);
    },
  );
});

describe("content chunk card — inactive for another reason", () => {
  it("says why when the store no longer keeps chunk vectors", async () => {
    await renderWith({
      ...CHUNKS_ON,
      content_chunks_effective: false,
      content_chunks_supported: false,
      content_chunks_disabled_reason: "unsupported",
    });
    expect(screen.getByText("contentChunks.inactiveBadge")).toBeTruthy();
    expect(screen.getByText("contentChunks.reasonUnsupported")).toBeTruthy();
    // Turning it off is still allowed.
    expect(chunkSwitch().hasAttribute("disabled")).toBe(false);
  });
});

describe("content chunk card — backfill progress", () => {
  it("shows the queue while memories are waiting", async () => {
    await renderWith({ ...CHUNKS_ON, content_chunks_backfill_pending: 42 });
    expect(
      screen.getByText("contentChunks.backfillPending(count=42)"),
    ).toBeTruthy();
  });

  it("says nothing once the queue is empty", async () => {
    await renderWith(CHUNKS_ON);
    expect(screen.queryByText(/contentChunks\.backfillPending/)).toBeNull();
  });

  it("re-reads the queue after saving", async () => {
    await renderWith(CHUNKS_OFF);
    mockGetConfig.mockResolvedValue({
      ...CHUNKS_ON,
      content_chunks_backfill_pending: 7,
    });
    fireEvent.click(chunkSwitch());
    fireEvent.click(screen.getByText("contentChunks.confirmEnable"));
    fireEvent.click(await screen.findByText("saveChanges"));
    await waitFor(() =>
      expect(
        screen.getByText("contentChunks.backfillPending(count=7)"),
      ).toBeTruthy(),
    );
  });
});
