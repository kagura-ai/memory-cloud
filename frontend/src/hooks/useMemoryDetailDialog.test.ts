/**
 * The supersede accept flow of useMemoryDetailDialog (#2427).
 *
 * The server says whether the caller may accept a suggestion
 * (`supersede_candidate.acceptance`); a refused one is never sent from here.
 * A click that loses a race is mapped by status, not server prose, to an
 * inline error, and the memory is re-read so the block shows the server's
 * current answer.
 *
 * #2600: the accept is `PATCH /memory/{id}` with `accept_supersede_candidate`
 * — update_memory's accept, which keeps the edge under the memory's author —
 * not `POST /graph/edges` under the caller.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { ApiError } from "@/lib/api/base";
import type { MemoryReference, SupersedeCandidate } from "@/lib/types/memory";

const mockReferenceMemory = vi.hoisted(() => vi.fn());
const mockUpdateMemoryById = vi.hoisted(() => vi.fn());
const mockCreateEdge = vi.hoisted(() => vi.fn());
const mockToast = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/memory", () => ({
  referenceMemory: (...a: unknown[]) => mockReferenceMemory(...a),
  updateMemoryById: (...a: unknown[]) => mockUpdateMemoryById(...a),
}));

// The generic edge writer: the accept must not reach it any more (#2600).
vi.mock("@/lib/api/graph", () => ({
  graphApi: { createEdge: (...a: unknown[]) => mockCreateEdge(...a) },
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const stableT = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
}));

import {
  useMemoryDetailDialog,
  type UseMemoryDetailDialogOpts,
} from "./useMemoryDetailDialog";

const MEMORY_ID = "11111111-1111-1111-1111-111111111111";
const TARGET_ID = "99999999-9999-9999-9999-999999999999";

function candidate(
  overrides: Partial<SupersedeCandidate> = {},
): SupersedeCandidate {
  return {
    memory_id: TARGET_ID,
    summary: "Older duplicate memory",
    similarity: 0.91,
    detected_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function reference(
  supersede: SupersedeCandidate | null = candidate({
    acceptance: { allowed: true },
  }),
): MemoryReference {
  return {
    memory_id: MEMORY_ID,
    summary: "Newer memory",
    context_summary: null,
    content: "body",
    details: null,
    type: "note",
    scope: "working",
    importance: 0.5,
    tags: [],
    context: null,
    created_at: "2026-07-02T00:00:00Z",
    updated_at: "2026-07-02T00:00:00Z",
    client: "mcp",
    supersede_candidate: supersede,
  };
}

function apiError(status: number, error: string) {
  return new ApiError({ status, error, message: "server prose" });
}

// The PATCH reply after an accept: the suggestion cleared, the new link listed.
function acceptedReply(): MemoryReference {
  return {
    ...reference(null),
    outgoing_links: [
      {
        memory_id: TARGET_ID,
        summary: "Older duplicate memory",
        type: "note",
        importance: 0.5,
        weight: 1,
        created_at: "2026-07-01T00:00:00Z",
      },
    ],
  };
}

async function openDialog(
  first: MemoryReference,
  onSupersedeAccepted?: UseMemoryDetailDialogOpts["onSupersedeAccepted"],
) {
  mockReferenceMemory.mockResolvedValueOnce(first);
  const hook = renderHook(() =>
    useMemoryDetailDialog({
      memoryIdParam: null,
      setMemoryIdParam: vi.fn(),
      onSupersedeAccepted,
    }),
  );
  act(() => hook.result.current.openDetail(MEMORY_ID));
  await waitFor(() => expect(hook.result.current.detailOpen).toBe(true));
  return hook;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useMemoryDetailDialog — supersede accept (#2427)", () => {
  it("does not send the accept when the server refuses it", async () => {
    const { result } = await openDialog(
      reference(
        candidate({ acceptance: { allowed: false, reason: "not_owner" } }),
      ),
    );

    act(() => result.current.acceptSupersede());

    expect(mockUpdateMemoryById).not.toHaveBeenCalled();
    expect(result.current.supersedeAccepting).toBe(false);
  });

  // #2518: the click's 409 reported before it — a different declared edge
  // already links the pair, so the action is disabled and never sent.
  it("does not send the accept when another declared edge links the pair", async () => {
    const { result } = await openDialog(
      reference(
        candidate({ acceptance: { allowed: false, reason: "edge_exists" } }),
      ),
    );

    act(() => result.current.acceptSupersede());

    expect(mockUpdateMemoryById).not.toHaveBeenCalled();
    expect(result.current.supersedeAccepting).toBe(false);
    expect(result.current.supersedeError).toBeNull();
    expect(result.current.supersedeCandidate?.acceptance?.reason).toBe(
      "edge_exists",
    );
  });

  it.each([
    [403, "AUTH-101"],
    [404, "RES-001"],
    [409, "RES-002"],
    [410, "RES-003"],
    [422, "VALIDATION-001"],
  ])(
    "re-reads after a %s without recording an access (#2518)",
    async (status, code) => {
      const { result } = await openDialog(reference());
      mockUpdateMemoryById.mockRejectedValueOnce(apiError(status, code));
      mockReferenceMemory.mockResolvedValueOnce(reference());

      act(() => result.current.acceptSupersede());

      await waitFor(() => expect(mockReferenceMemory).toHaveBeenCalledTimes(2));
      expect(mockReferenceMemory.mock.calls).toEqual([
        [MEMORY_ID],
        [MEMORY_ID, { recordAccess: false }],
      ]);
      await waitFor(() =>
        expect(result.current.supersedeAccepting).toBe(false),
      );
    },
  );

  it("maps a 403 to the denied message and re-reads the memory", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(403, "AUTH-101"));
    const refused = candidate({
      acceptance: { allowed: false, reason: "not_owner" },
    });
    mockReferenceMemory.mockResolvedValueOnce(reference(refused));

    act(() => result.current.acceptSupersede());

    await waitFor(() =>
      expect(result.current.supersedeCandidate?.acceptance).toEqual(
        refused.acceptance,
      ),
    );
    expect(result.current.supersedeError).toEqual({
      title: "supersede.errors.denied",
    });
    // #2518: the first read counts as an adoption, the re-read does not.
    expect(mockReferenceMemory).toHaveBeenNthCalledWith(1, MEMORY_ID);
    expect(mockReferenceMemory).toHaveBeenLastCalledWith(MEMORY_ID, {
      recordAccess: false,
    });
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("maps a 404 to the stale message and re-reads the memory", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(404, "RES-001"));
    mockReferenceMemory.mockResolvedValueOnce(reference(null));

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.supersedeCandidate).toBeNull());
    expect(result.current.supersedeError).toEqual({
      title: "supersede.errors.stale",
    });
    expect(mockReferenceMemory).toHaveBeenCalledTimes(2);
  });

  it("maps a 410 (the memory was forgotten) to the stale message", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(410, "RES-003"));
    mockReferenceMemory.mockRejectedValueOnce(apiError(404, "RES-001"));

    act(() => result.current.acceptSupersede());

    await waitFor(() =>
      expect(result.current.supersedeError).toEqual({
        title: "supersede.errors.stale",
      }),
    );
  });

  // #2600: the server refused the accept (422): no live suggestion, not the
  // caller's own, or a candidate newer than the memory. The re-read tells
  // the two outcomes apart, as for a 404.
  it("maps a 422 whose re-read has no suggestion to the stale message", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(
      apiError(422, "VALIDATION-001"),
    );
    mockReferenceMemory.mockResolvedValueOnce(reference(null));

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.supersedeCandidate).toBeNull());
    expect(result.current.supersedeError).toEqual({
      title: "supersede.errors.stale",
    });
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("maps a 422 whose re-read names another candidate to the stale message", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(
      apiError(422, "VALIDATION-001"),
    );
    const replaced = candidate({
      memory_id: "88888888-8888-8888-8888-888888888888",
      acceptance: { allowed: true },
    });
    mockReferenceMemory.mockResolvedValueOnce(reference(replaced));

    act(() => result.current.acceptSupersede());

    await waitFor(() =>
      expect(result.current.supersedeError).toEqual({
        title: "supersede.errors.stale",
      }),
    );
    expect(result.current.supersedeCandidate?.memory_id).toBe(
      replaced.memory_id,
    );
  });

  it.each(["not_owner", "newer_candidate"] as const)(
    "maps a 422 whose re-read still shows the suggestion (%s) to the denied message",
    async (reason) => {
      const { result } = await openDialog(reference());
      mockUpdateMemoryById.mockRejectedValueOnce(
        apiError(422, "VALIDATION-001"),
      );
      const refused = candidate({ acceptance: { allowed: false, reason } });
      mockReferenceMemory.mockResolvedValueOnce(reference(refused));

      act(() => result.current.acceptSupersede());

      await waitFor(() =>
        expect(result.current.supersedeError).toEqual({
          title: "supersede.errors.denied",
        }),
      );
      expect(result.current.supersedeCandidate?.acceptance).toEqual(
        refused.acceptance,
      );
    },
  );

  it("reads a 404 with the suggestion still there as a refusal, not a stale one", async () => {
    // A member downgraded to viewer: the route's deny is a uniform 404, the
    // re-read still shows the suggestion, now not writable.
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(404, "RES-001"));
    const notWritable = candidate({
      acceptance: { allowed: false, reason: "not_writable" },
    });
    mockReferenceMemory.mockResolvedValueOnce(reference(notWritable));

    act(() => result.current.acceptSupersede());

    await waitFor(() =>
      expect(result.current.supersedeError).toEqual({
        title: "supersede.errors.denied",
      }),
    );
    expect(result.current.supersedeCandidate?.acceptance).toEqual(
      notWritable.acceptance,
    );
  });

  it("maps a 409 to the conflict message that blocks a retry", async () => {
    // An edge declared after the memory was read: the re-read may still allow
    // it (or say edge_exists); the error itself keeps the action disabled.
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(409, "RES-002"));
    mockReferenceMemory.mockResolvedValueOnce(reference());

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(mockReferenceMemory).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(result.current.supersedeError).toEqual({
        title: "supersede.errors.conflict",
        blocksRetry: true,
      }),
    );
    expect(result.current.supersedeCandidate?.acceptance?.allowed).toBe(true);
  });

  it("clears an accept failure when an edit's reply replaces the memory", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(409, "RES-002"));
    mockReferenceMemory.mockResolvedValueOnce(reference());
    act(() => result.current.acceptSupersede());
    await waitFor(() => expect(result.current.supersedeError).not.toBeNull());

    act(() => result.current.applyEditSuccess(reference()));

    expect(result.current.supersedeError).toBeNull();
  });

  it("shows any other failure with the server's text and no re-read", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(new Error("network down"));

    act(() => result.current.acceptSupersede());

    await waitFor(() =>
      expect(result.current.supersedeError).toEqual({
        title: "supersede.acceptFailed",
        description: "network down",
      }),
    );
    expect(mockReferenceMemory).toHaveBeenCalledTimes(1);
    expect(result.current.supersedeAccepting).toBe(false);
  });

  it("hides the suggestion and toasts on success, clearing a previous error", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(new Error("blip"));
    act(() => result.current.acceptSupersede());
    await waitFor(() => expect(result.current.supersedeError).not.toBeNull());

    mockUpdateMemoryById.mockResolvedValueOnce(acceptedReply());
    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.supersedeCandidate).toBeNull());
    expect(result.current.supersedeError).toBeNull();
    expect(mockToast).toHaveBeenCalledWith({ title: "supersede.accepted" });
  });

  // #2600: update_memory's accept, so the edge lands under the memory's
  // author; the generic edge route (under the caller) is not used.
  it("accepts through the memory's PATCH, not the edge route", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockResolvedValueOnce(acceptedReply());

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.supersedeAccepting).toBe(false));
    expect(mockUpdateMemoryById).toHaveBeenCalledTimes(1);
    expect(mockUpdateMemoryById).toHaveBeenCalledWith(MEMORY_ID, {
      accept_supersede_candidate: true,
    });
    expect(mockCreateEdge).not.toHaveBeenCalled();
  });

  it("shows the PATCH reply: the suggestion cleared and the new link", async () => {
    const { result } = await openDialog(reference());
    const reply = acceptedReply();
    mockUpdateMemoryById.mockResolvedValueOnce(reply);

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.hydrated).toEqual(reply));
    expect(result.current.linkedRefs.outgoing).toEqual(reply.outgoing_links);
    expect(result.current.supersedeCandidate).toBeNull();
    // No re-read: the reply is the memory as it now stands.
    expect(mockReferenceMemory).toHaveBeenCalledTimes(1);
  });

  it("keeps the reopened view when the accept's reply lands after it", async () => {
    const { result } = await openDialog(reference());
    let resolve: (ref: MemoryReference) => void = () => {};
    mockUpdateMemoryById.mockReturnValueOnce(
      new Promise<MemoryReference>((r) => {
        resolve = r;
      }),
    );

    act(() => result.current.acceptSupersede());
    act(() => result.current.handleDetailOpenChange(false));
    const reopened = { ...reference(), summary: "Reopened view" };
    mockReferenceMemory.mockResolvedValueOnce(reopened);
    act(() => result.current.openDetail(MEMORY_ID));
    await waitFor(() => expect(result.current.hydrated).toEqual(reopened));
    await act(async () => resolve(acceptedReply()));

    expect(result.current.hydrated).toEqual(reopened);
    // The accepted pair still hides on the reopened view: the edge exists.
    expect(result.current.supersedeCandidate).toBeNull();
    expect(mockToast).toHaveBeenCalledWith({ title: "supersede.accepted" });
  });

  // #2515: the caller's hook to refresh what it shows (the Memories tab
  // re-runs its cached recall), fired once the hook has applied the accept.
  it("tells the caller which pair was accepted, after its own state", async () => {
    const onAccepted = vi.fn();
    const { result } = await openDialog(reference(), onAccepted);
    mockUpdateMemoryById.mockResolvedValueOnce(acceptedReply());

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1));
    expect(onAccepted).toHaveBeenCalledWith({
      sourceId: MEMORY_ID,
      targetId: TARGET_ID,
    });
    expect(result.current.supersedeCandidate).toBeNull();
    expect(mockToast.mock.invocationCallOrder[0]).toBeLessThan(
      onAccepted.mock.invocationCallOrder[0],
    );
  });

  it.each([
    // A refused accept re-reads the memory; any other failure does not.
    ["a refused accept (409)", () => apiError(409, "RES-002"), true],
    ["a refused accept (422)", () => apiError(422, "VALIDATION-001"), true],
    ["any other failure", () => new Error("network down"), false],
  ])(
    "does not tell the caller about %s",
    async (_label, makeError, rereads) => {
      const onAccepted = vi.fn();
      const { result } = await openDialog(reference(), onAccepted);
      mockUpdateMemoryById.mockRejectedValueOnce(makeError());
      if (rereads) mockReferenceMemory.mockResolvedValueOnce(reference());

      act(() => result.current.acceptSupersede());

      await waitFor(() =>
        expect(result.current.supersedeError).not.toBeNull(),
      );
      await waitFor(() =>
        expect(result.current.supersedeAccepting).toBe(false),
      );
      expect(onAccepted).not.toHaveBeenCalled();
    },
  );

  function deferredReference() {
    let resolve: (ref: MemoryReference) => void = () => {};
    mockReferenceMemory.mockReturnValueOnce(
      new Promise<MemoryReference>((r) => {
        resolve = r;
      }),
    );
    return (ref: MemoryReference) => resolve(ref);
  }

  it("ignores a re-read that resolves after an edit was saved", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(403, "AUTH-101"));
    const resolveReread = deferredReference();

    act(() => result.current.acceptSupersede());
    await waitFor(() => expect(mockReferenceMemory).toHaveBeenCalledTimes(2));
    const edited = { ...reference(), summary: "Edited summary", version: 2 };
    act(() => result.current.applyEditSuccess(edited));
    await act(async () => {
      resolveReread(
        reference(
          candidate({ acceptance: { allowed: false, reason: "not_owner" } }),
        ),
      );
    });

    expect(result.current.hydrated).toEqual(edited);
    expect(result.current.supersedeError).toBeNull();
    expect(result.current.supersedeAccepting).toBe(false);
  });

  it("ignores a re-read that resolves after the dialog was closed and reopened", async () => {
    const { result } = await openDialog(reference());
    mockUpdateMemoryById.mockRejectedValueOnce(apiError(403, "AUTH-101"));
    const resolveReread = deferredReference();

    act(() => result.current.acceptSupersede());
    await waitFor(() => expect(mockReferenceMemory).toHaveBeenCalledTimes(2));
    act(() => result.current.handleDetailOpenChange(false));
    const reopened = { ...reference(), summary: "Reopened view" };
    mockReferenceMemory.mockResolvedValueOnce(reopened);
    act(() => result.current.openDetail(MEMORY_ID));
    await waitFor(() => expect(result.current.hydrated).toEqual(reopened));
    await act(async () => {
      resolveReread(
        reference(
          candidate({ acceptance: { allowed: false, reason: "not_owner" } }),
        ),
      );
    });

    expect(result.current.hydrated).toEqual(reopened);
    expect(result.current.supersedeError).toBeNull();
  });

  it("drops a failure that lands after the dialog was closed", async () => {
    const { result } = await openDialog(reference());
    let reject: (e: unknown) => void = () => {};
    mockUpdateMemoryById.mockReturnValueOnce(
      new Promise((_, r) => {
        reject = r;
      }),
    );

    act(() => result.current.acceptSupersede());
    act(() => result.current.handleDetailOpenChange(false));
    await act(async () => {
      reject(apiError(403, "AUTH-101"));
    });

    expect(result.current.supersedeError).toBeNull();
    expect(mockReferenceMemory).toHaveBeenCalledTimes(1);
  });
});
