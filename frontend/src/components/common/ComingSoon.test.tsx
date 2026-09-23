/**
 * ComingSoon (#1646): the placeholder's own heading and sentence were
 * English literals on every locale.
 *
 * Rendered inside a real `NextIntlClientProvider` (not the key-echo mock), so
 * the assertions are on the sentences a reader actually sees, and a missing
 * key — which next-intl renders as the dotted key — fails the test.
 */

import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it } from "vitest";

import { ComingSoon } from "./ComingSoon";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const ENGLISH_LITERALS = [
  "Coming Soon",
  "This feature is currently under development and will be available in a future release.",
];

let intlErrors: string[] = [];

afterEach(() => {
  expect(intlErrors).toEqual([]);
  intlErrors = [];
});

function renderIn(locale: "en" | "ja") {
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "ja" ? ja : en}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${error.code}: ${error.message}`);
      }}
    >
      <ComingSoon
        title="Workspaces"
        description="Manage workspaces"
        featureDescription="Feature detail"
      />
    </NextIntlClientProvider>,
  );
}

describe("ComingSoon", () => {
  it("renders the Japanese heading and sentence, and no English literal", () => {
    renderIn("ja");

    expect(
      screen.getByRole("heading", { level: 2, name: "近日公開" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "この機能は現在開発中で、今後のリリースで利用可能になります。",
      ),
    ).toBeInTheDocument();

    const text = document.body.textContent ?? "";
    for (const literal of ENGLISH_LITERALS) {
      expect(text).not.toContain(literal);
    }
    expect(text).not.toContain("comingSoon.");
  });

  it("renders the English copy in English", () => {
    renderIn("en");

    expect(
      screen.getByRole("heading", { level: 2, name: "Coming Soon" }),
    ).toBeInTheDocument();
    expect(screen.getByText(ENGLISH_LITERALS[1])).toBeInTheDocument();
  });

  it("keeps the caller's page title, description and feature detail", () => {
    renderIn("ja");

    expect(
      screen.getByRole("heading", { level: 1, name: "Workspaces" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Manage workspaces")).toBeInTheDocument();
    expect(screen.getByText("Feature detail")).toBeInTheDocument();
  });
});
