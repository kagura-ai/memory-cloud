/**
 * The supersede accept flow of useMemoryDetailDialog (#2427).
 *
 * The server says whether the caller may accept a suggestion
 * (`supersede_candidate.acceptance`); a refused one never reaches the edge
 * route from here. A click that loses a race is mapped by status, not server
 * prose, to an inline error, and the memory is re-read so the block shows the
 * server's current answer.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { ApiError } from "@/lib/api/base";
import type { MemoryReference, SupersedeCandidate } from "@/lib/types/memory";

const mockReferenceMemory = vi.hoisted(() => vi.fn());
const mockCreateEdge = vi.hoisted(() => vi.fn());
const mockToast = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/memory", () => ({
  referenceMemory: (...a: unknown[]) => mockReferenceMemory(...a),
}));

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

import { useMemoryDetailDialog } from "./useMemoryDetailDialog";

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

async function openDialog(first: MemoryReference) {
  mockReferenceMemory.mockResolvedValueOnce(first);
  const hook = renderHook(() =>
    useMemoryDetailDialog({
      memoryIdParam: null,
      setMemoryIdParam: vi.fn(),
      contextId: "ctx-1",
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
  it("does not call the edge route when the server refuses the accept", async () => {
    const { result } = await openDialog(
      reference(
        candidate({ acceptance: { allowed: false, reason: "not_owner" } }),
      ),
    );

    act(() => result.current.acceptSupersede());

    expect(mockCreateEdge).not.toHaveBeenCalled();
    expect(result.current.supersedeAccepting).toBe(false);
  });

  it("maps a 403 to the denied message and re-reads the memory", async () => {
    const { result } = await openDialog(reference());
    mockCreateEdge.mockRejectedValueOnce(apiError(403, "AUTH-101"));
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
    expect(mockReferenceMemory).toHaveBeenLastCalledWith(MEMORY_ID);
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("maps a 404 to the stale message and re-reads the memory", async () => {
    const { result } = await openDialog(reference());
    mockCreateEdge.mockRejectedValueOnce(apiError(404, "RES-001"));
    mockReferenceMemory.mockResolvedValueOnce(reference(null));

    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.supersedeCandidate).toBeNull());
    expect(result.current.supersedeError).toEqual({
      title: "supersede.errors.stale",
    });
    expect(mockReferenceMemory).toHaveBeenCalledTimes(2);
  });

  it("reads a 404 with the suggestion still there as a refusal, not a stale one", async () => {
    // A member downgraded to viewer: the edge route's deny is a uniform 404,
    // the re-read still shows the suggestion, now not writable.
    const { result } = await openDialog(reference());
    mockCreateEdge.mockRejectedValueOnce(apiError(404, "RES-001"));
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
    // `acceptance` does not model another declared edge on the pair, so the
    // re-read still allows it: the error itself keeps the action disabled.
    const { result } = await openDialog(reference());
    mockCreateEdge.mockRejectedValueOnce(apiError(409, "RES-002"));
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
    mockCreateEdge.mockRejectedValueOnce(apiError(409, "RES-002"));
    mockReferenceMemory.mockResolvedValueOnce(reference());
    act(() => result.current.acceptSupersede());
    await waitFor(() => expect(result.current.supersedeError).not.toBeNull());

    act(() => result.current.applyEditSuccess(reference()));

    expect(result.current.supersedeError).toBeNull();
  });

  it("shows any other failure with the server's text and no re-read", async () => {
    const { result } = await openDialog(reference());
    mockCreateEdge.mockRejectedValueOnce(new Error("network down"));

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
    mockCreateEdge.mockRejectedValueOnce(new Error("blip"));
    act(() => result.current.acceptSupersede());
    await waitFor(() => expect(result.current.supersedeError).not.toBeNull());

    mockCreateEdge.mockResolvedValueOnce({ operation: "created" });
    act(() => result.current.acceptSupersede());

    await waitFor(() => expect(result.current.supersedeCandidate).toBeNull());
    expect(result.current.supersedeError).toBeNull();
    expect(mockCreateEdge).toHaveBeenLastCalledWith({
      context_id: "ctx-1",
      source_id: MEMORY_ID,
      target_id: TARGET_ID,
      edge_type: "supersedes",
    });
    expect(mockToast).toHaveBeenCalledWith({ title: "supersede.accepted" });
  });

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
    mockCreateEdge.mockRejectedValueOnce(apiError(403, "AUTH-101"));
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
    mockCreateEdge.mockRejectedValueOnce(apiError(403, "AUTH-101"));
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
    mockCreateEdge.mockReturnValueOnce(
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
