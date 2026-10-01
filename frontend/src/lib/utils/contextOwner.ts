/**
 * Who made a context, relative to the viewer (#1755).
 *
 * The Contexts list and the dashboard breakdown both show this, so the rule
 * lives once:
 * - `mine`    — the viewer created it.
 * - `shared`  — someone else did.
 * - `unknown` — no recorded creator (legacy rows, system-created defaults),
 *   or the viewer is not known yet (auth still hydrating). Without an id to
 *   compare, nothing may be called shared, and an owner filter never claims
 *   such a row for anyone.
 */

export type ContextOwnerKind = "mine" | "shared" | "unknown";

export function contextOwnerKind(
  createdBy: string | null | undefined,
  viewerId: string | null | undefined,
): ContextOwnerKind {
  if (!createdBy || !viewerId) return "unknown";
  return createdBy === viewerId ? "mine" : "shared";
}

/**
 * The label a page shows for a context's owner, given its translated
 * strings: "You", the creator's name, a stand-in for a creator who has no
 * display name, or a dash when nothing is known.
 */
export function contextOwnerLabel(
  kind: ContextOwnerKind,
  createdByName: string | null | undefined,
  labels: { you: string; unnamed: string },
): string {
  if (kind === "mine") return labels.you;
  if (kind === "shared") return createdByName || labels.unnamed;
  return "—";
}
