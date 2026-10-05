/**
 * MissingScopesNotice (#1758, #1880): the "reconnect Slack" notice in the
 * create dialog lists only the features whose scope the install lacks.
 *
 * Rendered with the REAL en / ja catalogues (the TeamConflictAlert.test.tsx
 * pattern) so a missing key or ICU argument fails here; `afterEach` fails the
 * test on any error the provider reports.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { MissingScopesNotice } from "./MissingScopesNotice";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const CATALOGUES = { en, ja } as const;
type Locale = keyof typeof CATALOGUES;

let intlErrors: string[] = [];

beforeEach(() => {
  intlErrors = [];
});

afterEach(() => {
  cleanup();
  expect(intlErrors).toEqual([]);
});

function renderIn(locale: Locale, ui: ReactElement) {
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={CATALOGUES[locale] as Record<string, unknown>}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${locale} ${error.code}: ${error.message}`);
      }}
    >
      {ui}
    </NextIntlClientProvider>,
  );
}

function lines() {
  return screen.getAllByRole("listitem").map((item) => item.textContent);
}

describe("MissingScopesNotice", () => {
  it("en, only groups:read missing: only the private-channel line", () => {
    renderIn("en", <MissingScopesNotice scopes={["groups:read"]} />);

    expect(lines()).toEqual([
      "groups:read — private channels in the channel picker",
    ]);
    expect(screen.queryByText(/slash command/)).not.toBeInTheDocument();
    expect(screen.queryByText(/@mention/)).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Make sure the Slack app requests this scope, then connect Slack again.",
      ),
    ).toBeInTheDocument();
  });

  it("ja, only groups:read missing: only the private-channel line", () => {
    renderIn("ja", <MissingScopesNotice scopes={["groups:read"]} />);

    expect(lines()).toEqual([
      "groups:read — チャンネル選択での private チャンネル一覧",
    ]);
    expect(screen.queryByText(/スラッシュコマンド/)).not.toBeInTheDocument();
    expect(screen.queryByText(/メンション/)).not.toBeInTheDocument();
    expect(
      screen.getByText("すべての機能を使うには Slack を再接続してください"),
    ).toBeInTheDocument();
  });

  it("en, the worker scopes missing: one line each, no private-channel line", () => {
    renderIn(
      "en",
      <MissingScopesNotice scopes={["commands", "app_mentions:read"]} />,
    );

    expect(lines()).toEqual([
      "commands — the slash command",
      "app_mentions:read — @mention replies",
    ]);
    expect(
      screen.getByText(
        "Make sure the Slack app requests these scopes, then connect Slack again.",
      ),
    ).toBeInTheDocument();
  });

  it("ja, all three missing", () => {
    renderIn(
      "ja",
      <MissingScopesNotice
        scopes={["commands", "app_mentions:read", "groups:read"]}
      />,
    );

    expect(lines()).toEqual([
      "commands — スラッシュコマンド",
      "app_mentions:read — @メンションへの応答",
      "groups:read — チャンネル選択での private チャンネル一覧",
    ]);
  });

  it.each(["en", "ja"] as const)(
    "%s: a scope this build does not know is still named",
    (locale) => {
      renderIn(locale, <MissingScopesNotice scopes={["files:read"]} />);

      expect(lines()).toEqual(["files:read"]);
    },
  );

  it("renders nothing without missing scopes", () => {
    const { container } = renderIn("en", <MissingScopesNotice scopes={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});
