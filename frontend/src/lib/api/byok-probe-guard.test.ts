import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Every surface that probes `/openai-key-status` must skip it when BYOK is off
 * (#1167).
 *
 * The route is behind `require_byok_enabled` and 404s in that configuration, so
 * an unguarded probe is a guaranteed-failing request on every render of that
 * page. Sidebar and the contexts page had the guard; the external-keys page did
 * not, because #1495 added a third caller and only two of the three were
 * copied from. Caught in review, pinned here — the failure mode is silent (a
 * 404 the UI swallows), so nothing else would notice a fourth caller repeating
 * it.
 *
 * Read as source rather than rendered: the point is a property shared across
 * three unrelated components, and asserting it once here is far cheaper than
 * three near-identical render tests — and it catches the NEXT caller too.
 */
const CALLERS = [
  "src/components/dashboard/Sidebar.tsx",
  "src/app/(authenticated)/workspace/contexts/page.tsx",
  "src/app/(authenticated)/workspace/integrations/external-keys/page.tsx",
];

describe("checkOpenAIKeyStatus callers (#1167 / #1495)", () => {
  it.each(CALLERS)("%s guards the probe on byokEnabled", (rel) => {
    const src = readFileSync(join(process.cwd(), rel), "utf8");
    if (!src.includes("checkOpenAIKeyStatus")) return; // not a caller (yet)
    expect(src).toContain("byokEnabled");
    expect(src).toMatch(/if \(!byokEnabled\) return;/);
  });

  it("finds no unguarded caller anywhere else", () => {
    // If a new surface starts probing, it belongs in CALLERS above with the
    // same guard — this keeps the list honest rather than quietly stale.
    const unlisted = CALLERS.filter((rel) => {
      const src = readFileSync(join(process.cwd(), rel), "utf8");
      return src.includes("checkOpenAIKeyStatus");
    });
    expect(unlisted.length).toBeGreaterThan(0);
  });
});
