/**
 * d3-force simulation presets for the bounded neural graph visualization.
 * Issue #233 — bounded neural graph visualization.
 */

export type PresetName = "default" | "collision" | "radial" | "loose";

export interface PresetConfig {
  readonly name: PresetName;
  readonly linkStrength: number;
  readonly linkDistance: number;
  readonly chargeStrength: number;
  readonly collisionRadius: number | null;
  readonly radialRadius: number | null;
  readonly alphaDecay: number;
}

export const FORCE_PRESETS: Readonly<Record<PresetName, PresetConfig>> =
  Object.freeze({
    default: Object.freeze({
      name: "default" as const,
      linkStrength: 0.3,
      linkDistance: 60,
      chargeStrength: -80,
      collisionRadius: null,
      radialRadius: null,
      alphaDecay: 0.0228,
    }),
    collision: Object.freeze({
      name: "collision" as const,
      linkStrength: 0.3,
      linkDistance: 60,
      chargeStrength: -80,
      collisionRadius: 14,
      radialRadius: null,
      alphaDecay: 0.0228,
    }),
    radial: Object.freeze({
      name: "radial" as const,
      linkStrength: 0.5,
      linkDistance: 50,
      chargeStrength: -120,
      collisionRadius: null,
      radialRadius: 180,
      alphaDecay: 0.0228,
    }),
    loose: Object.freeze({
      name: "loose" as const,
      linkStrength: 0.1,
      linkDistance: 120,
      chargeStrength: -200,
      collisionRadius: null,
      radialRadius: null,
      alphaDecay: 0.0228,
    }),
  });

export const PRESET_NAMES: readonly PresetName[] = Object.freeze(
  Object.keys(FORCE_PRESETS) as PresetName[],
);
