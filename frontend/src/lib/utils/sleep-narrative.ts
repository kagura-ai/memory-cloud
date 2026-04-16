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
  | "edgeDiscovery"
  | "dedup"
  | "importance"
  | "consolidation"
  | "reindex";

export interface NarrativePhaseResult {
  success: boolean;
  skipped: boolean;
  skip_reason: string | null;
  error: string | null;
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
    if (result.skip_reason) {
      return {
        key: "detail.narrative.skipped",
        values: { reason: result.skip_reason },
      };
    }
    return { key: "detail.narrative.skippedNoReason", values: {} };
  }

  const d = result.details;

  switch (phase) {
    case "edgeDiscovery": {
      const edges = num(d, "edges_created") ?? 0;
      const sampled = num(d, "sampled") ?? 0;
      if (edges === 0 && sampled === 0) {
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
      const deferred = num(d, "deferred_clusters") ?? 0;
      if (candidates === 0 && merged === 0) {
        return { key: "detail.narrative.phases.dedup.empty", values: {} };
      }
      const heldCount = Math.max(candidates - merged, 0);
      return {
        key: "detail.narrative.phases.dedup.success",
        values: {
          count: candidates,
          merged,
          held: heldCount,
          clusters,
          deferred,
        },
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
