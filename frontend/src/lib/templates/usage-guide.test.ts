/**
 * Starter templates describe the context; they do not direct the AI (#1698).
 *
 * The server returns a context's `summary` and `usage_guide` as the owner's
 * notes on what the context holds and how it is organised (#1682), so the
 * starting text the contexts page and the Settings tab fill in is written the
 * same way: "Bug fixes: type='bug-fix', importance 0.8+", not "Store bug
 * fixes with …" or "Always …". The text stays user-editable; this pins only
 * the starting copy, and that it still says how memories are typed and tagged.
 */
import { describe, expect, it } from "vitest";

import {
  CONTEXT_TEMPLATES,
  USAGE_GUIDE_TEMPLATES,
  getTemplate,
  getTemplatesByCategory,
} from "./usage-guide";

// A clause that opens with an imperative verb or a modal is a directive.
const DIRECTIVE_START =
  /^(always|never|you|use|keep|include|add|store|mark|review|update|record|track|describe|link|reference|follow|make sure|do not|don't|put|write|save|avoid|remember|tag|set|check|ensure)\b/i;
const MODAL = /\b(must|should|always|never|make sure|do not|don't)\b/i;

/** Lines, then sentences, with list bullets removed. */
function clauses(text: string): string[] {
  return text
    .split("\n")
    .flatMap((line) => line.split(/(?<=[.!?])\s+/))
    .map((clause) => clause.replace(/^\s*[-*]\s*/, "").trim())
    .filter(Boolean);
}

function directives(text: string): string[] {
  return clauses(text).filter(
    (clause) => DIRECTIVE_START.test(clause) || MODAL.test(clause),
  );
}

describe("directive detection", () => {
  // The pre-#1698 starter text: each must be caught.
  it.each([
    "Store code snippets with type='code' and tags=['language', 'framework'].",
    "Keep summaries concise (100-250 chars) for optimal search quality.",
    "- Include programming language (e.g., 'python', 'typescript')",
    "Review regularly and update importance as knowledge solidifies.",
    "Keep this private and mark sensitive topics with appropriate importance.",
    "Use tags: ['issue-XXX', 'backend', 'frontend']",
    "Personal coding projects and development notes. Track code snippets, bug fixes, and learning progress.",
  ])("flags %j", (text) => {
    expect(directives(text)).not.toEqual([]);
  });

  it.each([
    "Memories here are tagged by language and framework.",
    "- Bug fixes: type='bug-fix', importance 0.8+",
    "- Tagged with component names",
    "- Updated as tasks complete",
    "- Records who gave the advice",
    "Summaries are 100-250 characters.",
  ])("allows %j", (text) => {
    expect(directives(text)).toEqual([]);
  });
});

describe("CONTEXT_TEMPLATES (#1698)", () => {
  const filled = CONTEXT_TEMPLATES.filter((tpl) => tpl.id !== "empty");

  it.each(filled.map((tpl) => [tpl.id, tpl] as const))(
    "%s summary and usage_guide are descriptions, not directives",
    (_, tpl) => {
      expect(directives(tpl.summary)).toEqual([]);
      expect(directives(tpl.usage_guide)).toEqual([]);
    },
  );

  it.each(filled.map((tpl) => [tpl.id, tpl] as const))(
    "%s still says how its memories are typed and tagged",
    (_, tpl) => {
      expect(tpl.summary.trim()).not.toBe("");
      expect(tpl.usage_guide).toMatch(/type='[a-z-]+'/);
      expect(tpl.usage_guide).toMatch(/\btag/i);
    },
  );

  it("keeps the empty template empty", () => {
    expect(getTemplate("empty")).toMatchObject({
      summary: "",
      usage_guide: "",
    });
  });

  it("has unique ids and keeps the lookup helpers and alias", () => {
    const ids = CONTEXT_TEMPLATES.map((tpl) => tpl.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(getTemplate("kagura-dev")?.name).toBe(
      "Kagura Memory Cloud Development",
    );
    expect(getTemplatesByCategory("team").map((tpl) => tpl.id)).toEqual([
      "team-collab",
    ]);
    expect(USAGE_GUIDE_TEMPLATES).toBe(CONTEXT_TEMPLATES);
  });
});
