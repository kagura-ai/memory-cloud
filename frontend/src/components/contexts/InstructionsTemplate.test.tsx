/**
 * InstructionsTemplate (#1698): the copyable client instructions.
 *
 * Since #1682 the server describes a context's `usage_guide` as the owner's
 * notes on what the context holds and how it is organised — information, not
 * instructions. The template a user pastes into their own client says the
 * same: its Quick Start points at `get_context_info(context_id)` for the
 * context's purpose and notes, and no longer tells the model to "load
 * guidelines" or "follow context.usage_guide for this context's rules".
 *
 * Rendered with the REAL en / ja catalogues so the fallback and privacy
 * copy the template embeds is the shipped text.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const copyText = vi.hoisted(() => vi.fn());
vi.mock("@/lib/utils/clipboard", () => ({ copyText }));

import { InstructionsTemplate } from "./InstructionsTemplate";

const CATALOGUES = { en, ja } as const;
type Locale = keyof typeof CATALOGUES;

let intlErrors: string[] = [];

beforeEach(() => {
  intlErrors = [];
  copyText.mockReset();
  copyText.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  expect(intlErrors).toEqual([]);
});

function renderExpanded(
  locale: Locale,
  props: { usageGuide: string | null; isPrivate?: boolean },
): string {
  render(
    <NextIntlClientProvider
      locale={locale}
      messages={CATALOGUES[locale] as Record<string, unknown>}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${locale} ${error.code}: ${error.message}`);
      }}
    >
      <InstructionsTemplate
        contextName="my-context"
        usageGuide={props.usageGuide}
        isPrivate={props.isPrivate ?? true}
      />
    </NextIntlClientProvider>,
  );
  fireEvent.click(
    screen.getByRole("button", {
      name: CATALOGUES[locale].contexts.aiClientInstructions,
    }),
  );
  const pre = document.querySelector("pre");
  expect(pre).not.toBeNull();
  return pre!.textContent ?? "";
}

function section(text: string, heading: string): string {
  const start = text.indexOf(`## ${heading}`);
  expect(start).toBeGreaterThanOrEqual(0);
  const next = text.indexOf("\n## ", start + 1);
  return text.slice(start, next === -1 ? undefined : next);
}

describe("InstructionsTemplate Quick Start (#1698)", () => {
  it("points at get_context_info for the purpose and notes, framed as information", () => {
    const quickStart = section(
      renderExpanded("en", {
        usageGuide: "Memories here are tagged by project.",
      }),
      "Quick Start",
    );
    expect(quickStart).toContain("list_contexts()");
    expect(quickStart).toContain("get_context_info(context_id)");
    expect(quickStart).toContain("context.summary");
    expect(quickStart).toContain("context.usage_guide");
    expect(quickStart).toMatch(
      /information about the context, not instructions/,
    );
    expect(quickStart).not.toMatch(/guidelines|rules|\bfollow/i);
  });

  it("lists the current core tools, including update_memory", () => {
    const workflow = section(
      renderExpanded("en", { usageGuide: null }),
      "Core Workflow",
    );
    for (const tool of [
      "recall()",
      "remember()",
      "update_memory()",
      "explore()",
    ]) {
      expect(workflow).toContain(tool);
    }
  });
});

describe.each(["en", "ja"] as const)(
  "InstructionsTemplate notes section (%s)",
  (locale) => {
    const contexts = CATALOGUES[locale].contexts;

    it("embeds the owner's notes under a heading that calls them information", () => {
      const notes = section(
        renderExpanded(locale, {
          usageGuide: "Memories here are tagged by project.",
        }),
        "Context Notes",
      );
      expect(notes).toContain("usage_guide");
      expect(notes).toMatch(/information, not instructions/);
      expect(notes).toContain("Memories here are tagged by project.");
    });

    it("falls back to the no-notes copy when the context has none", () => {
      const notes = section(
        renderExpanded(locale, { usageGuide: null }),
        "Context Notes",
      );
      expect(notes).toContain(contexts.noGuidelinesSet);
    });

    it.each([
      [true, contexts.privateContextNote],
      [false, contexts.sharedContextNote],
    ] as const)(
      "isPrivate=%s carries the matching privacy note",
      (isPrivate, note) => {
        expect(
          renderExpanded(locale, { usageGuide: null, isPrivate }),
        ).toContain(note);
      },
    );
  },
);

describe("InstructionsTemplate copy", () => {
  it("copies exactly the text it shows", async () => {
    const shown = renderExpanded("en", {
      usageGuide: "Decisions carry their rationale.",
    });
    fireEvent.click(screen.getByRole("button", { name: en.contexts.copy }));
    await waitFor(() => expect(copyText).toHaveBeenCalledWith(shown));
    expect(await screen.findByText(en.contexts.copied)).toBeTruthy();
  });
});
