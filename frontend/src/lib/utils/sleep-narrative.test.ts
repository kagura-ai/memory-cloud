import { describe, expect, it } from "vitest";
import {
  buildHeadline,
  buildJudgeFailureNote,
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

  it("maps budget_exhausted to localized key", () => {
    const n = buildPhaseNarrative(
      "consolidation",
      phase({ skipped: true, skip_reason: "budget_exhausted" }),
    );
    expect(n?.key).toBe("detail.narrative.skipReasons.budgetExhausted");
  });

  it("maps disabled phase reasons to phaseDisabled key", () => {
    const n = buildPhaseNarrative(
      "dedup",
      phase({ skipped: true, skip_reason: "dedup_disabled" }),
    );
    expect(n?.key).toBe("detail.narrative.skipReasons.phaseDisabled");
  });

  it("maps sleep_mode_* reasons to sleepMode key with mode value", () => {
    const n = buildPhaseNarrative(
      "edgeDiscovery",
      phase({ skipped: true, skip_reason: "sleep_mode_edges_only" }),
    );
    expect(n?.key).toBe("detail.narrative.skipReasons.sleepMode");
    expect(n?.values.mode).toBe("edges_only");
  });

  it("falls back to skippedUnknown for unrecognized reasons", () => {
    const n = buildPhaseNarrative(
      "reindex",
      phase({ skipped: true, skip_reason: "some_future_reason" }),
    );
    expect(n?.key).toBe("detail.narrative.skippedUnknown");
    expect(n?.values.reason).toBe("some_future_reason");
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

    it("empty when edges_created is 0 even if sampled > 0", () => {
      const n = buildPhaseNarrative(
        "edgeDiscovery",
        phase({ details: { edges_created: 0, sampled: 20 } }),
      );
      expect(n?.key).toBe("detail.narrative.phases.edgeDiscovery.empty");
    });
  });

  describe("dedup", () => {
    it("builds success with candidates and merged", () => {
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
      expect(n?.values.clusters).toBe(5);
    });

    it("empty when neither candidates nor merged present", () => {
      const n = buildPhaseNarrative("dedup", phase({ details: {} }));
      expect(n?.key).toBe("detail.narrative.phases.dedup.empty");
    });

    it("uses successWithSplit when oversize clusters were split (#1190)", () => {
      const n = buildPhaseNarrative(
        "dedup",
        phase({
          details: {
            candidates: 782,
            merged: 4,
            clusters: 3,
            deferred_clusters: 1,
            oversize_clusters: 1,
            oversize_max_size: 40,
            split_subclusters: 3,
            deferred_pairs: 120,
          },
        }),
      );
      expect(n?.key).toBe("detail.narrative.phases.dedup.successWithSplit");
      expect(n?.values.count).toBe(782);
      expect(n?.values.merged).toBe(4);
      expect(n?.values.oversize).toBe(1);
      expect(n?.values.subclusters).toBe(3);
      expect(n?.values.deferredPairs).toBe(120);
    });

    it("legacy pre-v0.43.0 blob (deferred_clusters only) keeps plain success", () => {
      // No split happened on those runs — "split into 0 batches" would lie.
      const n = buildPhaseNarrative(
        "dedup",
        phase({
          details: {
            candidates: 12,
            merged: 0,
            clusters: 0,
            deferred_clusters: 2,
          },
        }),
      );
      expect(n?.key).toBe("detail.narrative.phases.dedup.success");
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

describe("buildJudgeFailureNote", () => {
  it("returns null for a null result", () => {
    expect(buildJudgeFailureNote(null)).toBeNull();
  });

  it("returns null when the field is absent (pre-v0.43.0 blob)", () => {
    expect(buildJudgeFailureNote(phase())).toBeNull();
  });

  it("returns null when zero calls failed", () => {
    expect(buildJudgeFailureNote(phase({ llm_call_failures: 0 }))).toBeNull();
  });

  it("builds the note with the failure count", () => {
    const n = buildJudgeFailureNote(phase({ llm_call_failures: 5 }));
    expect(n?.key).toBe("detail.narrative.judgeFailures");
    expect(n?.values.count).toBe(5);
  });
});
