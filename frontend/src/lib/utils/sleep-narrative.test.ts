import { describe, expect, it } from "vitest";
import {
  buildHeadline,
  buildPhaseNarrative,
  type NarrativePhaseResult,
} from "./sleep-narrative";

const HEADLINE_SRC = {
  memories_processed: 42,
  edges_created: 7,
  memories_merged: 3,
  memories_promoted: 1,
  memories_flagged: 1,
};

function phase(
  overrides: Partial<NarrativePhaseResult> = {},
): NarrativePhaseResult {
  return {
    success: true,
    skipped: false,
    skip_reason: null,
    error: null,
    details: null,
    ...overrides,
  };
}

describe("buildHeadline", () => {
  it("uses headline key when context name is present", () => {
    const n = buildHeadline(
      { context_name: "kagura-dev", context_deleted: false },
      HEADLINE_SRC,
    );
    expect(n.key).toBe("detail.narrative.headline");
    expect(n.values.contextName).toBe("kagura-dev");
    expect(n.values.processed).toBe(42);
    expect(n.values.merged).toBe(3);
  });

  it("uses headlineNoContext when context is absent", () => {
    const n = buildHeadline(
      { context_name: null, context_deleted: false },
      HEADLINE_SRC,
    );
    expect(n.key).toBe("detail.narrative.headlineNoContext");
    expect(n.values.contextName).toBeUndefined();
  });

  it("uses headlineDeletedContext when context_deleted is true", () => {
    const n = buildHeadline(
      { context_name: null, context_deleted: true },
      HEADLINE_SRC,
    );
    expect(n.key).toBe("detail.narrative.headlineDeletedContext");
    expect(n.values.contextName).toBeUndefined();
  });

  it("prefers context_name when both set (race: not expected but defined)", () => {
    const n = buildHeadline(
      { context_name: "kagura-dev", context_deleted: true },
      HEADLINE_SRC,
    );
    expect(n.key).toBe("detail.narrative.headline");
    expect(n.values.contextName).toBe("kagura-dev");
  });
});

describe("buildPhaseNarrative", () => {
  it("returns null when result is null", () => {
    expect(buildPhaseNarrative("edgeDiscovery", null)).toBeNull();
  });

  it("prefers error over skipped", () => {
    const n = buildPhaseNarrative(
      "dedup",
      phase({ error: "boom", skipped: true, skip_reason: "budget" }),
    );
    expect(n?.key).toBe("detail.narrative.failed");
    expect(n?.values.error).toBe("boom");
  });

  it("renders skipped with skip_reason", () => {
    const n = buildPhaseNarrative(
      "consolidation",
      phase({ skipped: true, skip_reason: "LLM budget exhausted" }),
    );
    expect(n?.key).toBe("detail.narrative.skipped");
    expect(n?.values.reason).toBe("LLM budget exhausted");
  });

  it("uses skippedNoReason key when skip_reason is null", () => {
    const n = buildPhaseNarrative("consolidation", phase({ skipped: true }));
    expect(n?.key).toBe("detail.narrative.skippedNoReason");
    expect(n?.values).toEqual({});
  });

  describe("edgeDiscovery", () => {
    it("builds success from edges_created and sampled", () => {
      const n = buildPhaseNarrative(
        "edgeDiscovery",
        phase({ details: { edges_created: 7, sampled: 20 } }),
      );
      expect(n?.key).toBe("detail.narrative.phases.edgeDiscovery.success");
      expect(n?.values.count).toBe(7);
      expect(n?.values.sampled).toBe(20);
    });

    it("empty when details lack both keys", () => {
      const n = buildPhaseNarrative("edgeDiscovery", phase({ details: {} }));
      expect(n?.key).toBe("detail.narrative.phases.edgeDiscovery.empty");
    });

    it("null-safe on missing details dict", () => {
      const n = buildPhaseNarrative("edgeDiscovery", phase({ details: null }));
      expect(n?.key).toBe("detail.narrative.phases.edgeDiscovery.empty");
    });
  });

  describe("dedup", () => {
    it("builds success and derives held count", () => {
      const n = buildPhaseNarrative(
        "dedup",
        phase({
          details: {
            candidates: 12,
            merged: 3,
            clusters: 5,
            deferred_clusters: 1,
          },
        }),
      );
      expect(n?.key).toBe("detail.narrative.phases.dedup.success");
      expect(n?.values.count).toBe(12);
      expect(n?.values.merged).toBe(3);
      expect(n?.values.held).toBe(9);
    });

    it("clamps held to 0 when merged exceeds candidates", () => {
      const n = buildPhaseNarrative(
        "dedup",
        phase({ details: { candidates: 2, merged: 5 } }),
      );
      expect(n?.values.held).toBe(0);
    });

    it("empty when neither candidates nor merged present", () => {
      const n = buildPhaseNarrative("dedup", phase({ details: {} }));
      expect(n?.key).toBe("detail.narrative.phases.dedup.empty");
    });
  });

  describe("importance", () => {
    it("recognizes the no_stale_memories sentinel", () => {
      const n = buildPhaseNarrative(
        "importance",
        phase({ details: { message: "no_stale_memories" } }),
      );
      expect(n?.key).toBe("detail.narrative.phases.importance.empty");
    });

    it("builds success with alpha formatted to 2 decimals", () => {
      const n = buildPhaseNarrative(
        "importance",
        phase({ details: { candidates: 10, updated: 4, alpha: 0.333 } }),
      );
      expect(n?.key).toBe("detail.narrative.phases.importance.success");
      expect(n?.values.alpha).toBe("0.33");
    });

    it("emits '-' for missing alpha", () => {
      const n = buildPhaseNarrative(
        "importance",
        phase({ details: { candidates: 1, updated: 0 } }),
      );
      expect(n?.values.alpha).toBe("-");
    });
  });

  describe("consolidation", () => {
    it("builds success from working_count + promoted + borderline", () => {
      const n = buildPhaseNarrative(
        "consolidation",
        phase({
          details: {
            working_count: 8,
            rule_promoted: 2,
            llm_promoted: 1,
            borderline: 5,
          },
        }),
      );
      expect(n?.key).toBe("detail.narrative.phases.consolidation.success");
      expect(n?.values.candidates).toBe(8);
      expect(n?.values.rulePromoted).toBe(2);
      expect(n?.values.llmPromoted).toBe(1);
      expect(n?.values.borderline).toBe(5);
    });

    it("empty when everything is zero/absent", () => {
      const n = buildPhaseNarrative("consolidation", phase({ details: {} }));
      expect(n?.key).toBe("detail.narrative.phases.consolidation.empty");
    });
  });

  describe("reindex", () => {
    it("recognizes the no_memories_to_reindex sentinel", () => {
      const n = buildPhaseNarrative(
        "reindex",
        phase({ details: { message: "no_memories_to_reindex" } }),
      );
      expect(n?.key).toBe("detail.narrative.phases.reindex.empty");
    });

    it("builds success with count and failed", () => {
      const n = buildPhaseNarrative(
        "reindex",
        phase({ details: { reindexed: 42, failed: 1 } }),
      );
      expect(n?.key).toBe("detail.narrative.phases.reindex.success");
      expect(n?.values.count).toBe(42);
      expect(n?.values.failed).toBe(1);
    });

    it("defaults failed to 0 when absent", () => {
      const n = buildPhaseNarrative(
        "reindex",
        phase({ details: { reindexed: 10 } }),
      );
      expect(n?.values.failed).toBe(0);
    });
  });
});
