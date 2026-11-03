/**
 * Tests for the remember() / recall() memory API client wrappers (Issue #952).
 *
 * The web app had no create-memory path before #952 (memory.ts was
 * read/reference/forget only); the first-run onboarding flow needs to save a
 * sample memory and recall it in-app. These verify the two new wrappers hit the
 * correct endpoints with the documented request shapes, against the backend
 * (`backend/src/api/routes/memory.py` + `backend/src/models/schemas.py`).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const mockPost = vi.fn();
const mockPatch = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: vi.fn(),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
  },
}));

import { rememberMemory, recallMemories, updateMemoryById } from "./memory";

const REMEMBER = "/api/v1/memory/remember";
const RECALL = "/api/v1/memory/recall";

beforeEach(() => {
  mockPost.mockReset();
  mockPatch.mockReset();
});

describe("rememberMemory", () => {
  it("POSTs the remember endpoint with the layered body", async () => {
    mockPost.mockResolvedValue({
      status: "success",
      memory_id: "11111111-1111-1111-1111-111111111111",
      scope: "persistent",
    });
    const res = await rememberMemory({
      summary: "Kagura recall blends 60% semantic + 40% full-text search.",
      content: "Hybrid search: dense vectors plus BM25, with optional rerank.",
      type: "note",
      importance: 0.7,
      context: { context_id: "ctx-1" },
    });
    expect(mockPost).toHaveBeenCalledWith(REMEMBER, {
      summary: "Kagura recall blends 60% semantic + 40% full-text search.",
      content: "Hybrid search: dense vectors plus BM25, with optional rerank.",
      type: "note",
      importance: 0.7,
      context: { context_id: "ctx-1" },
    });
    expect(res.memory_id).toBe("11111111-1111-1111-1111-111111111111");
    expect(res.status).toBe("success");
  });

  it("passes through only the provided optional fields", async () => {
    mockPost.mockResolvedValue({ status: "success", memory_id: "m", scope: "working" });
    await rememberMemory({
      summary: "a summary that is at least ten chars",
      content: "body",
      type: "note",
    });
    expect(mockPost).toHaveBeenCalledWith(REMEMBER, {
      summary: "a summary that is at least ten chars",
      content: "body",
      type: "note",
    });
  });
});

describe("recallMemories", () => {
  it("POSTs the recall endpoint with query + context filter", async () => {
    mockPost.mockResolvedValue({
      results: [
        {
          memory_id: "m1",
          summary: "Kagura recall blends 60% semantic + 40% full-text search.",
          context_summary: null,
          type: "note",
          importance: 0.7,
          scope: "persistent",
          created_at: "2026-06-13T00:00:00Z",
          client: "web",
          tags: [],
          context: null,
          score: 0.91,
        },
      ],
      related_tags: [],
    });
    const res = await recallMemories({
      query: "how does recall search work",
      k: 3,
      filters: { context_id: "ctx-1" },
    });
    expect(mockPost).toHaveBeenCalledWith(RECALL, {
      query: "how does recall search work",
      k: 3,
      filters: { context_id: "ctx-1" },
    });
    expect(res.results).toHaveLength(1);
    expect(res.results[0].score).toBe(0.91);
  });

  it("omits absent optional fields (query only)", async () => {
    mockPost.mockResolvedValue({ results: [], related_tags: [] });
    await recallMemories({ query: "anything" });
    expect(mockPost).toHaveBeenCalledWith(RECALL, { query: "anything" });
  });

  // #2132: the top-level include_superseded flag (RecallRequest) and the tag
  // filter reach the endpoint as given; `folded` comes back on the row.
  it("sends include_superseded and the tag filter, and returns folded", async () => {
    mockPost.mockResolvedValue({
      results: [
        {
          memory_id: "m-new",
          summary: "current value",
          context_summary: null,
          type: "fact",
          importance: 0.5,
          scope: "persistent",
          created_at: "2026-10-01T00:00:00Z",
          client: "api",
          tags: [],
          context: null,
          folded: { count: 1, memory_ids: ["m-old"] },
        },
      ],
    });
    const res = await recallMemories({
      query: "value",
      k: 20,
      include_superseded: true,
      filters: { context_id: "ctx-1", tags: ["a"], tags_match: "all" },
    });
    expect(mockPost).toHaveBeenCalledWith(RECALL, {
      query: "value",
      k: 20,
      include_superseded: true,
      filters: { context_id: "ctx-1", tags: ["a"], tags_match: "all" },
    });
    expect(res.results[0].folded).toEqual({ count: 1, memory_ids: ["m-old"] });
  });
});

// #2600: the web dialog's supersede accept is the memory's PATCH with the
// update_memory flag, and nothing else in the body (the server refuses an
// accept that carries a field edit).
describe("updateMemoryById", () => {
  it("PATCHes the memory with only the accept flag", async () => {
    mockPatch.mockResolvedValue({ memory_id: "m 1", supersede_candidate: null });
    const res = await updateMemoryById("m 1", {
      accept_supersede_candidate: true,
    });
    expect(mockPatch).toHaveBeenCalledWith("/api/v1/memory/m%201", {
      accept_supersede_candidate: true,
    });
    expect(res.supersede_candidate).toBeNull();
  });
});
