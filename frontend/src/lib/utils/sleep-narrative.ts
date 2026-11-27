/**
 * Pure helpers that turn Sleep Maintenance phase results into i18n-ready
 * narrative descriptors. Each descriptor is `{ key, values }` and is meant to
 * be consumed via `t(key, values)` on a `useTranslations("admin.sleepReports")`
 * scope.
 *
 * Keeping this layer pure keeps the detail page simple and lets future
 * enhancements (action samples, pipeline timelines, new phases) extend the
 * narrative without touching the page structure.
 */

export type PhaseName =
  "edgeDiscovery" | "dedup" | "importance" | "consolidation" | "reindex";

export interface NarrativePhaseResult {
  success: boolean;
  skipped: boolean;
  skip_reason: string | null;
  error: string | null;
  // #1183: judge calls that raised in this phase. Optional — pre-v0.43.0
  // report blobs don't carry it.
  llm_call_failures?: number;
  details: Record<string, unknown> | null;
}

export interface NarrativeHeadlineSource {
  memories_processed: number;
  edges_created: number;
  memories_merged: number;
  memories_promoted: number;
  memories_flagged: number;
}

export interface NarrativeHeadlineContext {
  context_name: string | null;
  context_deleted: boolean;
}

export interface Narrative {
  key: string;
  values: Record<string, string | number>;
}

function num(
  details: Record<string, unknown> | null,
  key: string,
): number | null {
  if (!details) return null;
  const v = details[key];
  return typeof v === "number" ? v : null;
}

function str(
  details: Record<string, unknown> | null,
  key: string,
): string | null {
  if (!details) return null;
  const v = details[key];
  return typeof v === "string" ? v : null;
}

/**
 * Build the top-of-page headline narrative.
 *
 * Routing by context state:
 *   - context.context_name set            → "headline" with the name
 *   - context.context_deleted true        → "headlineDeletedContext" (localized marker)
 *   - neither (workspace-wide run)        → "headlineNoContext"
 *
 * Localization of the "(deleted)" marker is the frontend's responsibility;
 * the backend only reports the boolean flag.
 */
export function buildHeadline(
  context: NarrativeHeadlineContext,
  report: NarrativeHeadlineSource,
): Narrative {
  const baseValues = {
    processed: report.memories_processed,
    merged: report.memories_merged,
    edges: report.edges_created,
    promoted: report.memories_promoted,
    flagged: report.memories_flagged,
  };
  if (context.context_name) {
    return {
      key: "detail.narrative.headline",
      values: { contextName: context.context_name, ...baseValues },
    };
  }
  if (context.context_deleted) {
    return {
      key: "detail.narrative.headlineDeletedContext",
      values: baseValues,
    };
  }
  return {
    key: "detail.narrative.headlineNoContext",
    values: baseValues,
  };
}

/**
 * Build a single-line narrative for a phase card.
 *
 * Returns null when the phase was not run (result === null). Callers should
 * treat null as "render nothing" rather than emitting an empty bullet.
 *
 * Precedence:
 *   1. `error`       → "Failed: {error}"
 *   2. `skipped`     → "Skipped: {reason}" (reason falls back to a generic string)
 *   3. phase-specific `success` (or `empty` if nothing to report)
 */
export function buildPhaseNarrative(
  phase: PhaseName,
  result: NarrativePhaseResult | null,
): Narrative | null {
  if (result === null) return null;

  if (result.error) {
    return {
      key: "detail.narrative.failed",
      values: { error: result.error },
    };
  }

  if (result.skipped) {
    if (!result.skip_reason) {
      return { key: "detail.narrative.skippedNoReason", values: {} };
    }
    const knownReasons: Record<string, string> = {
      budget_exhausted: "detail.narrative.skipReasons.budgetExhausted",
      edge_discovery_disabled: "detail.narrative.skipReasons.phaseDisabled",
      importance_reeval_disabled: "detail.narrative.skipReasons.phaseDisabled",
      dedup_disabled: "detail.narrative.skipReasons.phaseDisabled",
    };
    const key = knownReasons[result.skip_reason];
    if (key) {
      return { key, values: {} };
    }
    if (result.skip_reason.startsWith("sleep_mode_")) {
      return {
        key: "detail.narrative.skipReasons.sleepMode",
        values: { mode: result.skip_reason.replace("sleep_mode_", "") },
      };
    }
    return {
      key: "detail.narrative.skippedUnknown",
      values: { reason: result.skip_reason },
    };
  }

  const d = result.details;

  switch (phase) {
    case "edgeDiscovery": {
      const edges = num(d, "edges_created") ?? 0;
      const sampled = num(d, "sampled") ?? 0;
      if (edges === 0) {
        return {
          key: "detail.narrative.phases.edgeDiscovery.empty",
          values: {},
        };
      }
      return {
        key: "detail.narrative.phases.edgeDiscovery.success",
        values: { count: edges, sampled },
      };
    }
    case "dedup": {
      const candidates = num(d, "candidates") ?? 0;
      const merged = num(d, "merged") ?? 0;
      const clusters = num(d, "clusters") ?? 0;
      // #1184: oversize mega-clusters are split into judgeable subclusters;
      // report the split + the deferred (unjudged) pair volume so "0 merges
      // because everything was deferred" is visible. Keyed on the NEW
      // `oversize_clusters` only — pre-v0.43.0 blobs carry the legacy
      // `deferred_clusters` (wholesale deferral, no split happened), and
      // rendering "split into 0 batches" for those would be a lie; they
      // keep the plain success line.
      const oversize = num(d, "oversize_clusters") ?? 0;
      const subclusters = num(d, "split_subclusters") ?? 0;
      const deferredPairs = num(d, "deferred_pairs") ?? 0;
      if (candidates === 0 && merged === 0) {
        return { key: "detail.narrative.phases.dedup.empty", values: {} };
      }
      if (oversize > 0) {
        return {
          key: "detail.narrative.phases.dedup.successWithSplit",
          values: {
            count: candidates,
            merged,
            oversize,
            subclusters,
            deferredPairs,
          },
        };
      }
      return {
        key: "detail.narrative.phases.dedup.success",
        values: { count: candidates, merged, clusters },
      };
    }
    case "importance": {
      const message = str(d, "message");
      if (message === "no_stale_memories") {
        return { key: "detail.narrative.phases.importance.empty", values: {} };
      }
      const candidates = num(d, "candidates");
      const updated = num(d, "updated");
      if (candidates === null && updated === null) {
        return { key: "detail.narrative.phases.importance.empty", values: {} };
      }
      const alpha = num(d, "alpha");
      return {
        key: "detail.narrative.phases.importance.success",
        values: {
          candidates: candidates ?? 0,
          updated: updated ?? 0,
          alpha: alpha === null ? "-" : alpha.toFixed(2),
        },
      };
    }
    case "consolidation": {
      const working = num(d, "working_count") ?? 0;
      const rulePromoted = num(d, "rule_promoted") ?? 0;
      const llmPromoted = num(d, "llm_promoted") ?? 0;
      const borderline = num(d, "borderline") ?? 0;
      if (
        working === 0 &&
        rulePromoted === 0 &&
        llmPromoted === 0 &&
        borderline === 0
      ) {
        return {
          key: "detail.narrative.phases.consolidation.empty",
          values: {},
        };
      }
      return {
        key: "detail.narrative.phases.consolidation.success",
        values: {
          candidates: working,
          rulePromoted,
          llmPromoted,
          borderline,
        },
      };
    }
    case "reindex": {
      const message = str(d, "message");
      if (message === "no_memories_to_reindex") {
        return { key: "detail.narrative.phases.reindex.empty", values: {} };
      }
      const reindexed = num(d, "reindexed") ?? 0;
      const failed = num(d, "failed") ?? 0;
      if (reindexed === 0 && failed === 0) {
        return { key: "detail.narrative.phases.reindex.empty", values: {} };
      }
      return {
        key: "detail.narrative.phases.reindex.success",
        values: { count: reindexed, failed },
      };
    }
  }
}

/**
 * #3019: the message key of each phase name the API can report in
 * `budget_limited_phases` (the four phases that call a model and the related
 * index). Any other name has no label and is shown as it is.
 */
const BUDGET_PHASE_LABEL_KEYS: Record<string, string> = {
  edge_discovery: "budget.phases.edgeDiscovery",
  related_index: "budget.phases.relatedIndex",
  dedup_merge: "budget.phases.dedup",
  importance_reeval: "budget.phases.importance",
  consolidation: "budget.phases.consolidation",
};

/** The label key of a budget-limited phase name, or null to show it raw. */
export function budgetPhaseLabelKey(phase: string): string | null {
  return Object.prototype.hasOwnProperty.call(BUDGET_PHASE_LABEL_KEYS, phase)
    ? BUDGET_PHASE_LABEL_KEYS[phase]
    : null;
}

export interface BudgetNoteLine {
  key: string;
  /** Phase names in run order; label them with `budgetPhaseLabelKey`. */
  phases: string[];
}

/**
 * Build the run-level budget note (#3019): one line per kind present in
 * `budget_limited_phases`, the skipped phases first, each naming its phases
 * in run order. Returns [] when the map is empty or absent (older reports).
 */
export function buildBudgetNote(
  limited: Record<string, string> | null | undefined,
): BudgetNoteLine[] {
  if (!limited) return [];
  const entries = Object.entries(limited);
  const lines: BudgetNoteLine[] = [];
  const skipped = entries.filter(([, kind]) => kind === "skipped");
  const cut = entries.filter(([, kind]) => kind === "cut");
  if (skipped.length > 0) {
    lines.push({
      key: "detail.narrative.budgetSkipped",
      phases: skipped.map(([phase]) => phase),
    });
  }
  if (cut.length > 0) {
    lines.push({
      key: "detail.narrative.budgetCut",
      phases: cut.map(([phase]) => phase),
    });
  }
  return lines;
}

/**
 * Build the budget sub-note for a phase card (#3019): the phase stopped
 * because its budget ran out, with work left. Reads `budget_cut`, or the
 * consolidation judge's `llm_pass_cut_for_budget` on older reports. Returns
 * null otherwise.
 */
export function buildBudgetCutNote(
  result: NarrativePhaseResult | null,
): Narrative | null {
  const d = result?.details;
  if (!d) return null;
  if (d.budget_cut === true || d.llm_pass_cut_for_budget === true) {
    return { key: "detail.narrative.budgetCutPhase", values: {} };
  }
  return null;
}

/**
 * Build the judge-failure sub-note for a phase card (#1183 / #1190).
 *
 * A phase whose judge-LLM calls partially failed still renders a plain
 * success narrative; this companion note surfaces the failure count so a
 * `degraded` run is explainable at the phase level. Returns null when the
 * phase was not run, the field is absent (pre-v0.43.0 blobs), or no calls
 * failed.
 */
export function buildJudgeFailureNote(
  result: NarrativePhaseResult | null,
): Narrative | null {
  const failures = result?.llm_call_failures;
  if (typeof failures !== "number" || failures <= 0) return null;
  return {
    key: "detail.narrative.judgeFailures",
    values: { count: failures },
  };
}
