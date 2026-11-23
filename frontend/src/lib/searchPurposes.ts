/**
 * Search purposes (#2983): named sets of a context's hybrid weights and
 * fusion, so an owner can pick by what the context holds instead of by the
 * values. The purpose is not stored anywhere: the search settings show the
 * purpose whose values equal the current ones, or Custom.
 */

import type {
  ContextFusion,
  ContextSearchConfig,
  ContextSearchConfigUpdate,
} from "@/lib/api/contexts";

export type SearchPurpose = "conversation" | "documents" | "custom";

/** A purpose that carries values (Custom carries none). */
export type PresetSearchPurpose = Exclude<SearchPurpose, "custom">;

export interface SearchPurposeValues {
  semantic_weight: number;
  bm25_weight: number;
  fusion: ContextFusion;
}

export const SEARCH_PURPOSE_PRESETS: Readonly<
  Record<PresetSearchPurpose, Readonly<SearchPurposeValues>>
> = {
  conversation: { semantic_weight: 0.6, bm25_weight: 0.4, fusion: "auto" },
  documents: { semantic_weight: 0.8, bm25_weight: 0.2, fusion: "auto" },
};

/** The purpose a new context gets: today's defaults. */
export const DEFAULT_SEARCH_PURPOSE: PresetSearchPurpose = "conversation";

/** The order the select lists them in. */
export const SEARCH_PURPOSES: readonly SearchPurpose[] = [
  "conversation",
  "documents",
  "custom",
];

export const PRESET_SEARCH_PURPOSES: readonly PresetSearchPurpose[] = [
  "conversation",
  "documents",
];

const round2 = (value: number) => Math.round(value * 100) / 100;

/**
 * The purpose whose values equal the given ones, else Custom. Weights are
 * compared after rounding to two places, the fusion exactly. A `fusion` of
 * `undefined` (a server that does not report it) matches on the weights only.
 */
export function matchSearchPurpose(values: {
  semantic_weight: number;
  bm25_weight: number;
  fusion?: ContextFusion;
}): SearchPurpose {
  for (const purpose of PRESET_SEARCH_PURPOSES) {
    const preset = SEARCH_PURPOSE_PRESETS[purpose];
    if (
      round2(values.semantic_weight) === preset.semantic_weight &&
      round2(values.bm25_weight) === preset.bm25_weight &&
      (values.fusion === undefined || values.fusion === preset.fusion)
    ) {
      return purpose;
    }
  }
  return "custom";
}

/**
 * The search-config update that applies a purpose on top of the stored
 * config: the fields the update requires at their stored values, the
 * purpose's weights, and its fusion only where the server reports one.
 */
export function searchPurposeUpdate(
  purpose: PresetSearchPurpose,
  stored: ContextSearchConfig,
): ContextSearchConfigUpdate {
  const preset = SEARCH_PURPOSE_PRESETS[purpose];
  return {
    semantic_weight: preset.semantic_weight,
    bm25_weight: preset.bm25_weight,
    fetch_factor: stored.fetch_factor,
    use_rerank: stored.use_rerank,
    reranker_provider: stored.reranker_provider,
    reranker_model: stored.reranker_model,
    ...(stored.fusion !== undefined ? { fusion: preset.fusion } : {}),
  };
}
