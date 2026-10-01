/**
 * TeamConflictAlert (#1753): the "Slack team already connected" refusal in
 * the create dialog.
 *
 * Rendered with the REAL en / ja catalogues (the ContextPrivacyChoice.test.tsx
 * pattern) so a missing key or an ICU argument the copy needs fails here;
 * `afterEach` fails the test on any error the provider reports.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  TeamConflictAlert,
  teamConflictOf,
  type TeamConflict,
} from "./TeamConflictAlert";
import { ApiError } from "@/lib/api/base";
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

function conflict409(details: Record<string, unknown>) {
  return new ApiError({
    error: "RES-002",
    message: "server English that must not be shown",
    status: 409,
    details,
  });
}

const HERE: TeamConflict = {
  kind: "here",
  connectorId: "11111111-1111-1111-1111-111111111111",
  displayName: "Acme Slack",
};

describe("teamConflictOf", () => {
  it("reads the here reason with the existing connector", () => {
    expect(
      teamConflictOf(
        conflict409({
          reason: "connector_team_connected_here",
          connector_id: HERE.kind === "here" ? HERE.connectorId : "",
          display_name: "Acme Slack",
        }),
      ),
    ).toEqual(HERE);
  });

  it("reads the elsewhere reason", () => {
    expect(
      teamConflictOf(
        conflict409({ reason: "connector_team_connected_elsewhere" }),
      ),
    ).toEqual({ kind: "elsewhere" });
  });

  it("ignores other errors", () => {
    expect(teamConflictOf(conflict409({}))).toBeNull();
    expect(
      teamConflictOf(conflict409({ reason: "connector_team_connected_here" })),
    ).toBeNull(); // no connector id to point at
    expect(
      teamConflictOf(
        new ApiError({
          error: "RES-002",
          message: "x",
          status: 400,
          details: { reason: "connector_team_connected_elsewhere" },
        }),
      ),
    ).toBeNull();
    expect(teamConflictOf(new Error("boom"))).toBeNull();
  });
});

describe("TeamConflictAlert", () => {
  it("ja, here: names the connector and hands over to its editor", () => {
    const onEdit = vi.fn();
    renderIn(
      "ja",
      <TeamConflictAlert conflict={HERE} onEditExisting={onEdit} />,
    );

    expect(
      screen.getByText(
        "この Slack ワークスペースは、このワークスペースのコネクタ『Acme Slack』で接続済みです。今回のサインインで Slack の権限は更新され、チャンネル一覧には 1 分以内に反映されます。設定を変えるときは既存のコネクタを編集してください。",
      ),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "既存のコネクタを編集" }),
    );
    expect(onEdit).toHaveBeenCalledOnce();
  });

  it("ja, elsewhere: the issue's copy, no edit action", () => {
    renderIn("ja", <TeamConflictAlert conflict={{ kind: "elsewhere" }} />);

    expect(
      screen.getByText(
        "この Slack ワークスペースは、別の Kagura ワークスペースで接続されています。1 つの Slack ワークスペースに接続できる Kagura ワークスペースは 1 つだけです。移す場合は元のワークスペースでコネクタを削除してください。共有する場合は、元のワークスペースのメンバーに招待してもらってください。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("en, elsewhere with the Slack workspace name", () => {
    renderIn(
      "en",
      <TeamConflictAlert conflict={{ kind: "elsewhere" }} teamName="Acme" />,
    );

    expect(
      screen.getByText(
        /^The Slack workspace “Acme” is already connected to another Kagura workspace\./,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/can be connected to only one Kagura workspace/),
    ).toBeInTheDocument();
  });

  it("en, here with the Slack name; no edit action when the connector is not loaded", () => {
    renderIn("en", <TeamConflictAlert conflict={HERE} teamName="Acme" />);

    expect(
      screen.getByText(
        "The Slack workspace “Acme” is already connected in this workspace by the connector “Acme Slack”. Its Slack permissions were refreshed by this sign-in; the channel list picks that up within a minute. To change its settings, edit the existing connector.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("ja, here with the Slack name", () => {
    renderIn("ja", <TeamConflictAlert conflict={HERE} teamName="Acme" />);

    expect(
      screen.getByText(/^Slack ワークスペース「Acme」は、/),
    ).toBeInTheDocument();
  });
});
