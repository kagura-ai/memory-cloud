/**
 * Tests for the workspace allowance client (#2919, account API of #2918).
 *
 * Pins the wire contract of the four session-only account routes: the paths,
 * the methods and the request bodies (`scenario` on the preview,
 * `expected_version` on the save). The page builds on these shapes.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPut = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    put: (...args: unknown[]) => mockPut(...args),
  },
}));

import {
  getWorkspaceSlots,
  listSlotWorkspaceContexts,
  previewWorkspaceSlots,
  saveWorkspaceSelection,
  type WorkspaceSlots,
} from "./workspaceSlots";

const BASE = "/api/v1/me/workspace-slots";

const SLOTS: WorkspaceSlots = {
  allowance: {
    cap: 1,
    base: 1,
    slot_bonus: 0,
    tier_grant: 0,
    tier: "free",
    next_tier: "pro",
    cap_on_next_tier: 3,
  },
  owned_count: 3,
  active_count: 1,
  active_slots: 1,
  contracted_count: 0,
  selection_required: true,
  selection: { workspace_ids: [], saved_at: null },
  version: 4,
  if_paid_plans_end: null,
  workspaces: [
    {
      id: "w1",
      name: "Personal",
      plan_name: "free",
      status: "active",
      suspended_at: null,
      under_billing_contract: false,
      is_current: true,
      created_at: "2026-03-02T09:14:00Z",
    },
  ],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
});

describe("getWorkspaceSlots", () => {
  it("GETs the account route and returns the body", async () => {
    mockGet.mockResolvedValue(SLOTS);
    await expect(getWorkspaceSlots()).resolves.toEqual(SLOTS);
    expect(mockGet).toHaveBeenCalledWith(BASE);
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
  });
});

describe("previewWorkspaceSlots", () => {
  it("POSTs the ids and the scenario, and never a version", async () => {
    const preview = {
      scenario: "paid_plans_end",
      cap: 1,
      active_slots: 1,
      active: ["w3"],
      suspended: ["w1", "w2"],
      unmet: [],
      changes: { suspend: ["w1", "w2"], resume: ["w3"] },
    };
    mockPost.mockResolvedValue(preview);

    await expect(
      previewWorkspaceSlots(["w3"], "paid_plans_end"),
    ).resolves.toEqual(preview);
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/preview`, {
      workspace_ids: ["w3"],
      scenario: "paid_plans_end",
    });
  });

  it("sends the current scenario for a resume preview", async () => {
    mockPost.mockResolvedValue({});
    await previewWorkspaceSlots(["w2", "w1"], "current");
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/preview`, {
      workspace_ids: ["w2", "w1"],
      scenario: "current",
    });
  });
});

describe("saveWorkspaceSelection", () => {
  it("PUTs the ordered ids with the expected version", async () => {
    mockPut.mockResolvedValue(SLOTS);
    await expect(saveWorkspaceSelection(["w2", "w1"], 4)).resolves.toEqual(
      SLOTS,
    );
    expect(mockPut).toHaveBeenCalledWith(`${BASE}/selection`, {
      workspace_ids: ["w2", "w1"],
      expected_version: 4,
    });
  });

  it("propagates a stale-version refusal to the caller", async () => {
    const err = Object.assign(new Error("stale"), {
      status: 409,
      error: "SLOT-003",
    });
    mockPut.mockRejectedValue(err);
    await expect(saveWorkspaceSelection([], 3)).rejects.toBe(err);
  });
});

describe("listSlotWorkspaceContexts", () => {
  it("GETs the contexts of one owned workspace, with the id encoded", async () => {
    const body = {
      workspace_id: "w 2",
      contexts: [{ id: "c1", name: "Papers", is_private: false }],
    };
    mockGet.mockResolvedValue(body);
    await expect(listSlotWorkspaceContexts("w 2")).resolves.toEqual(body);
    expect(mockGet).toHaveBeenCalledWith(`${BASE}/workspaces/w%202/contexts`);
  });
});
