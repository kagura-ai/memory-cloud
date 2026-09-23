/**
 * Tests for QuotaWarning (Issue #1647 — the block rendered untranslated
 * English copy on every locale).
 *
 * WHY THIS FILE RENDERS WITH THE REAL TRANSLATOR: the house fixture mocks
 * `useTranslations` as `() => (key) => key`, which would pass just as happily
 * against hardcoded English. These cases mount the component inside a real
 * `NextIntlClientProvider` loaded with ja.json, so the assertions are on the
 * Japanese sentences a Japanese user actually sees — and on the absence of the
 * English literals this issue removed.
 */

import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { QuotaWarning } from "./QuotaWarning";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

/** The resource noun a Japanese caller passes (UsageStats sends t("memories")). */
const RESOURCE_JA = ja.usageStats.memories;

/**
 * Literals that lived in the component before #1647. None of them may reach
 * the DOM in a Japanese render — including via a missing key, since next-intl
 * falls back to the key (not to English) when one is absent.
 */
const ENGLISH_LITERALS = [
  ...Object.values(en.quotaWarning),
  "Quota Exceeded",
  "Critical: Approaching Limit",
  "Warning: Quota Usage High",
  "You have exceeded your quota limit",
  "You are very close to your",
  "Upgrade Plan",
];

function renderJa(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="ja" messages={ja} timeZone="UTC">
      {ui}
    </NextIntlClientProvider>,
  );
}

function expectNoEnglish() {
  const text = document.body.textContent ?? "";
  for (const literal of ENGLISH_LITERALS) {
    expect(text).not.toContain(literal);
  }
  // A raw key leaking through (e.g. "quotaWarning.titleWarning") means the
  // message is missing, which reads as broken UI rather than as a translation.
  expect(text).not.toContain("quotaWarning.");
}

describe("QuotaWarning i18n", () => {
  it("en.json and ja.json define the same quotaWarning keys", () => {
    expect(Object.keys(ja.quotaWarning).sort()).toEqual(
      Object.keys(en.quotaWarning).sort(),
    );
  });

  it("renders nothing below the 80% threshold", () => {
    const { container } = renderJa(
      <QuotaWarning current={79} limit={100} resourceLabel={RESOURCE_JA} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the Japanese warning title at 80% and no upgrade CTA yet", () => {
    const onUpgrade = vi.fn();
    renderJa(
      <QuotaWarning
        current={80}
        limit={100}
        resourceLabel={RESOURCE_JA}
        onUpgrade={onUpgrade}
      />,
    );

    expect(screen.getByText(ja.quotaWarning.titleWarning)).toBeInTheDocument();
    // 80–95% is the quiet tier: progress only, no sentence, no CTA.
    expect(
      screen.queryByRole("button", { name: ja.quotaWarning.upgrade }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/80\.0%/)).toBeInTheDocument();
    expectNoEnglish();
  });

  it("shows the Japanese critical copy and CTA at 95%", () => {
    const onUpgrade = vi.fn();
    renderJa(
      <QuotaWarning
        current={95}
        limit={100}
        resourceLabel={RESOURCE_JA}
        onUpgrade={onUpgrade}
      />,
    );

    expect(screen.getByText(ja.quotaWarning.titleCritical)).toBeInTheDocument();
    expect(
      screen.getByText(
        ja.quotaWarning.bodyCritical.replace("{resource}", RESOURCE_JA),
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: ja.quotaWarning.upgrade }),
    ).toBeInTheDocument();
    expectNoEnglish();
  });

  it("shows the Japanese exceeded copy and CTA at 100%", () => {
    const onUpgrade = vi.fn();
    renderJa(
      <QuotaWarning
        current={100}
        limit={100}
        resourceLabel={RESOURCE_JA}
        onUpgrade={onUpgrade}
      />,
    );

    expect(screen.getByText(ja.quotaWarning.titleExceeded)).toBeInTheDocument();
    expect(
      screen.getByText(
        ja.quotaWarning.bodyExceeded.replace("{resource}", RESOURCE_JA),
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: ja.quotaWarning.upgrade }),
    ).toBeInTheDocument();
    expectNoEnglish();
  });

  it("interpolates the caller's noun verbatim instead of lowercasing it", () => {
    // The pre-#1647 component spliced `label.toLowerCase()` into an English
    // sentence, which cannot work in Japanese and mangles the noun in English.
    render(
      <NextIntlClientProvider locale="en" messages={en} timeZone="UTC">
        <QuotaWarning current={100} limit={100} resourceLabel="Memories" />
      </NextIntlClientProvider>,
    );

    const body = screen.getByText(
      en.quotaWarning.bodyExceeded.replace("{resource}", "Memories"),
    );
    expect(body).toBeInTheDocument();
    expect(body.textContent).not.toContain("memories");
  });
});
