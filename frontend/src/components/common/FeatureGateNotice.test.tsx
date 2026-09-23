/**
 * FeatureGateNotice (#1646).
 *
 * WHY THIS FILE RENDERS WITH THE REAL TRANSLATOR: the house fixture mocks
 * `useTranslations` as `() => (key) => key`, which never formats a message.
 * The failure this component exists to prevent is a message that
 * interpolates an argument its branch does not have (a `{plan}` for a gate no
 * tier lifts, `{limit}` for a quota refusal that carries no counts): next-intl
 * then raises FORMATTING_ERROR and renders the raw key. Every render here goes
 * through a `NextIntlClientProvider` loaded with the real en / ja catalogue,
 * and `afterEach` fails the test on any error the provider reports.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import {
  createTranslator,
  NextIntlClientProvider,
  type Messages as IntlMessages,
} from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FeatureGateNotice,
  featureGateText,
  featureGateToast,
  gateMessageKeys,
  GATE_NOUN_FORM,
  type FeatureGateScope,
  type FeatureGateText,
  type GateTranslator,
} from "./FeatureGateNotice";
import { WorkspaceRole } from "@/lib/auth/rbac";
import type {
  FeatureGate,
  FeatureGateState,
  RefusedGateState,
} from "@/lib/gates/featureGates";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const mockPush = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

type Messages = Record<string, unknown>;
type GateKeyOf = FeatureGate["feature"];
const CATALOGUES = { en, ja } as const;
type Locale = keyof typeof CATALOGUES;
const LOCALES: readonly Locale[] = ["en", "ja"];

const REFUSED: readonly RefusedGateState[] = [
  "plan",
  "quota",
  "deployment",
  "role",
  "allowlist",
];

let intlErrors: string[] = [];

beforeEach(() => {
  intlErrors = [];
  mockPush.mockReset();
});

afterEach(() => {
  // A message formatted without an argument it needs lands here.
  expect(intlErrors).toEqual([]);
});

function renderIn(locale: Locale, ui: ReactElement) {
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={CATALOGUES[locale] as Messages}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${locale} ${error.code}: ${error.message}`);
      }}
    >
      {ui}
    </NextIntlClientProvider>,
  );
}

/**
 * The `gate`-namespace translator, throwing on any formatting error. Typed
 * with next-intl's default (un-augmented) message type — the same one
 * `useTranslations("gate")` hands the component.
 */
function gateT(locale: Locale): GateTranslator {
  return createTranslator({
    locale,
    messages: CATALOGUES[locale] as IntlMessages,
    namespace: "gate",
    onError: (error) => {
      throw error;
    },
  });
}

function gateOf(
  g: Partial<FeatureGate> & { readonly state: FeatureGateState },
): FeatureGate {
  return { feature: "resources", canUpgrade: false, ...g };
}

/** One of each variant, each in its own wrapper so a test can query it. */
function AllVariants({
  gate,
  scope,
}: {
  gate: FeatureGate;
  scope?: FeatureGateScope;
}) {
  return (
    <>
      <div data-testid="inline">
        <FeatureGateNotice gate={gate} scope={scope} />
      </div>
      <div data-testid="page">
        <FeatureGateNotice variant="page" gate={gate} scope={scope} />
      </div>
      <div data-testid="control">
        <FeatureGateNotice variant="control" gate={gate} scope={scope} />
      </div>
      <div data-testid="control-description">
        <FeatureGateNotice
          variant="control"
          gate={gate}
          scope={scope}
          detail="description"
          showBadge={false}
        />
      </div>
    </>
  );
}

const VARIANT_IDS = [
  "inline",
  "page",
  "control",
  "control-description",
] as const;

/** Read every slot back out of the rendered variants. */
function renderedText(): FeatureGateText {
  const alert = within(screen.getByTestId("inline")).getByRole("alert");
  const page = screen.getByTestId("page");
  const control = screen.getByTestId("control");
  const controlDescription = screen.getByTestId("control-description");

  const title = alert.querySelector("h5")?.textContent;
  const description = alert.querySelector("div > span")?.textContent;
  const action = within(alert).queryByRole("button")?.textContent ?? null;

  // The other variants render the same selection.
  expect(page.querySelector("h3")?.textContent).toBe(title);
  expect(page.querySelector("p")?.textContent).toBe(description);
  expect(within(page).queryByRole("button")?.textContent ?? null).toBe(action);
  expect(within(control).queryByRole("button")?.textContent ?? null).toBe(
    action,
  );
  expect(controlDescription.querySelector("p")?.textContent).toBe(description);
  // showBadge={false} and no children: no badge row at all.
  expect(controlDescription.querySelector("div.flex")).toBeNull();

  return {
    title: title ?? "",
    description: description ?? "",
    action,
    badge: control.querySelector("div.flex")?.textContent ?? null,
    hint: control.querySelector("p")?.textContent ?? "",
  };
}

// ── Every key-selection branch, formatted through the real catalogues ──────

interface Branch {
  readonly name: string;
  readonly gate: FeatureGate;
  readonly scope?: FeatureGateScope;
  readonly expected: Readonly<Record<Locale, FeatureGateText>>;
}

const noTier = gateOf({ state: "plan", canUpgrade: true });
const withTier = gateOf({
  state: "plan",
  requiredPlan: "promax",
  planLabel: "XL",
  currentPlan: "basic",
  currentPlanLabel: "M",
  canUpgrade: true,
});
const NO_TIER_TEXT: Readonly<Record<Locale, FeatureGateText>> = {
  en: {
    title: "No plan includes resources",
    description: "No plan on this deployment includes resources.",
    action: null,
    badge: null,
    hint: "Not available on any plan",
  },
  ja: {
    title: "リソース は利用できません",
    description: "この環境では リソース はどのプランでも利用できません。",
    action: null,
    badge: null,
    hint: "どのプランでも利用できません",
  },
};

const BRANCHES: readonly Branch[] = [
  {
    name: "plan, no tier lifts it (titleNoTier / descriptionNoTier / hintNoTier, no CTA even when canUpgrade)",
    gate: noTier,
    expected: NO_TIER_TEXT,
  },
  {
    name: "plan, no tier lifts it, scope create (same copy — there is nothing new to require)",
    gate: noTier,
    scope: "create",
    expected: NO_TIER_TEXT,
  },
  {
    name: "plan, scope all",
    gate: withTier,
    expected: {
      en: {
        title: "The XL plan includes resources",
        description: "Upgrade to the XL plan to use resources.",
        action: "Upgrade to XL",
        badge: "XL plan",
        hint: "Requires the XL plan",
      },
      ja: {
        title: "リソース は XL プランで利用できます",
        description:
          "リソース を利用するには XL プランにアップグレードしてください。",
        action: "XL にアップグレード",
        badge: "XL プラン",
        hint: "XL プランが必要です",
      },
    },
  },
  {
    name: "plan, scope create (newTitle / newDescription)",
    gate: withTier,
    scope: "create",
    expected: {
      en: {
        title: "New resources require the XL plan",
        description:
          "Existing resources keep working. Creating new resources is available on the XL plan.",
        action: "Upgrade to XL",
        badge: "XL plan",
        hint: "Requires the XL plan",
      },
      ja: {
        title: "新規 リソース には XL プランが必要です",
        description:
          "既存の リソース は引き続き利用できます。新規 リソース の作成には XL プランが必要です。",
        action: "XL にアップグレード",
        badge: "XL プラン",
        hint: "XL プランが必要です",
      },
    },
  },
  {
    name: "quota with counts, current tier and a tier that raises it (description + upsell)",
    gate: gateOf({
      state: "quota",
      feature: "contexts",
      requiredPlan: "basic",
      planLabel: "M",
      currentPlan: "free",
      currentPlanLabel: "S",
      current: 1,
      limit: 1,
      canUpgrade: true,
    }),
    expected: {
      en: {
        title: "You've reached the context limit",
        description:
          "Your S plan's context limit is 1, and current usage is 1. The M plan raises this limit.",
        action: "View plans",
        badge: "Limit reached",
        hint: "Limit reached",
      },
      ja: {
        title: "コンテキスト の上限に達しました",
        description:
          "S プランでは コンテキスト の上限は 1 です（現在 1）。M プランにすると上限が上がります。",
        action: "プランを見る",
        badge: "上限に達しました",
        hint: "上限に達しました",
      },
    },
  },
  {
    name: "quota whose tier-raising sentence is withheld: the member cannot upgrade",
    gate: gateOf({
      state: "quota",
      feature: "members",
      requiredPlan: "basic",
      planLabel: "M",
      currentPlan: "free",
      currentPlanLabel: "S",
      current: 3,
      limit: 3,
      canUpgrade: false,
    }),
    expected: {
      en: {
        title: "You've reached the member limit",
        description: "Your S plan's member limit is 3, and current usage is 3.",
        action: null,
        badge: "Limit reached",
        hint: "Limit reached",
      },
      ja: {
        title: "メンバー の上限に達しました",
        description: "S プランでは メンバー の上限は 3 です（現在 3）。",
        action: null,
        badge: "上限に達しました",
        hint: "上限に達しました",
      },
    },
  },
  {
    // Not a shape the producers emit (canUpgrade on a quota gate implies a
    // required tier), but the message selection must not depend on that: the
    // upsell sentence needs {plan}, so with no tier it is not selected.
    name: "quota with canUpgrade but no tier to name (no upsell sentence, CTA kept)",
    gate: gateOf({
      state: "quota",
      feature: "storage",
      currentPlan: "free",
      currentPlanLabel: "S",
      current: 10,
      limit: 10,
      canUpgrade: true,
    }),
    expected: {
      en: {
        title: "You've reached the storage limit",
        description:
          "Your S plan's storage limit is 10, and current usage is 10.",
        action: "View plans",
        badge: "Limit reached",
        hint: "Limit reached",
      },
      ja: {
        title: "ストレージ の上限に達しました",
        description: "S プランでは ストレージ の上限は 10 です（現在 10）。",
        action: "プランを見る",
        badge: "上限に達しました",
        hint: "上限に達しました",
      },
    },
  },
  {
    name: "quota with counts but no current tier (descriptionNoPlan)",
    gate: gateOf({
      state: "quota",
      feature: "agents",
      current: 5,
      limit: 5,
    }),
    expected: {
      en: {
        title: "You've reached the agent limit",
        description: "The agent limit is 5, and current usage is 5.",
        action: null,
        badge: "Limit reached",
        hint: "Limit reached",
      },
      ja: {
        title: "エージェント の上限に達しました",
        description: "エージェント の上限は 5 です（現在 5）。",
        action: null,
        badge: "上限に達しました",
        hint: "上限に達しました",
      },
    },
  },
  {
    name: "quota with no counts — QUOTA-002 / rate limits (descriptionNoNumbers, even with a current tier)",
    gate: gateOf({
      state: "quota",
      feature: "embedding_spend",
      currentPlan: "free",
      currentPlanLabel: "S",
    }),
    expected: {
      en: {
        title: "You've reached the embedding spend limit",
        description: "Usage has reached the embedding spend limit.",
        action: null,
        badge: "Limit reached",
        hint: "Limit reached",
      },
      ja: {
        title: "埋め込み使用額 の上限に達しました",
        description: "埋め込み使用額 が上限に達しています。",
        action: null,
        badge: "上限に達しました",
        hint: "上限に達しました",
      },
    },
  },
  {
    name: "deployment (no CTA even when canUpgrade)",
    gate: gateOf({
      state: "deployment",
      feature: "cost_dashboard",
      canUpgrade: true,
    }),
    expected: {
      en: {
        title: "This deployment does not offer the cost dashboard",
        description:
          "This Kagura Memory Cloud deployment has the cost dashboard turned off. Ask the administrator of this deployment if you need access.",
        action: null,
        badge: "Not available",
        hint: "Not available on this deployment",
      },
      ja: {
        title: "この環境では コストダッシュボード は利用できません",
        description:
          "この Kagura Memory Cloud 環境では コストダッシュボード が無効化されています。必要な場合は環境の管理者にお問い合わせください。",
        action: null,
        badge: "利用不可",
        hint: "この環境では利用できません",
      },
    },
  },
  {
    name: "role, owner minimum (no CTA even when canUpgrade)",
    gate: gateOf({
      state: "role",
      feature: "memory_analysis",
      requiredRole: WorkspaceRole.Owner,
      canUpgrade: true,
    }),
    expected: {
      en: {
        title: "Only the workspace owner can do this",
        description:
          "Managing Memory Analysis is limited to the workspace owner.",
        action: null,
        badge: "Owner only",
        hint: "Owner only",
      },
      ja: {
        title: "ワークスペースのオーナーのみが実行できます",
        description:
          "メモリー分析 の管理はワークスペースのオーナーに限定されています。",
        action: null,
        badge: "オーナーのみ",
        hint: "オーナーのみ",
      },
    },
  },
  {
    name: "role, admin minimum",
    gate: gateOf({
      state: "role",
      feature: "team_invitations",
      requiredRole: WorkspaceRole.Admin,
    }),
    expected: {
      en: {
        title: "Only a workspace owner or admin can do this",
        description:
          "Managing team invitations is limited to workspace owners and admins.",
        action: null,
        badge: "Owner or admin only",
        hint: "Owner or admin only",
      },
      ja: {
        title: "ワークスペースのオーナーまたは管理者のみが実行できます",
        description:
          "チーム招待 の管理はワークスペースのオーナーと管理者に限定されています。",
        action: null,
        badge: "オーナー/管理者のみ",
        hint: "オーナー/管理者のみ",
      },
    },
  },
  {
    name: "role with no known minimum falls to the stricter owner copy",
    gate: gateOf({ state: "role", feature: "members" }),
    expected: {
      en: {
        title: "Only the workspace owner can do this",
        description: "Managing members is limited to the workspace owner.",
        action: null,
        badge: "Owner only",
        hint: "Owner only",
      },
      ja: {
        title: "ワークスペースのオーナーのみが実行できます",
        description:
          "メンバー の管理はワークスペースのオーナーに限定されています。",
        action: null,
        badge: "オーナーのみ",
        hint: "オーナーのみ",
      },
    },
  },
  {
    name: "allowlist (plan-neutral, no CTA even when canUpgrade)",
    gate: gateOf({
      state: "allowlist",
      feature: "memory_analysis",
      canUpgrade: true,
    }),
    expected: {
      en: {
        title: "This workspace does not have access to Memory Analysis yet",
        description:
          "Access to Memory Analysis is being rolled out gradually. Reach out if you would like early access.",
        action: null,
        badge: "Not enabled",
        hint: "Not enabled for this workspace",
      },
      ja: {
        title: "このワークスペースでは メモリー分析 はまだ有効化されていません",
        description:
          "メモリー分析 は段階的に提供しています。ご利用をご希望の場合はお問い合わせください。",
        action: null,
        badge: "未有効化",
        hint: "このワークスペースでは未有効化",
      },
    },
  },
];

describe.each(LOCALES)("FeatureGateNotice key selection (%s)", (locale) => {
  it.each(BRANCHES.map((b) => [b.name, b] as const))(
    "%s",
    (_, { gate, scope, expected }) => {
      renderIn(locale, <AllVariants gate={gate} scope={scope} />);

      expect(renderedText()).toEqual(expected[locale]);
      // A FORMATTING_ERROR falls back to the dotted key.
      expect(document.body.textContent).not.toMatch(/gate\.|features\./);

      // The toast formats the same selection.
      const toastArgs = featureGateToast(gate, gateT(locale), scope);
      expect(toastArgs).toEqual({
        title: expected[locale].title,
        description: expected[locale].description,
        ...(gate.state === "quota" ? { variant: "destructive" } : {}),
      });
    },
  );
});

// ── Hard rules ──────────────────────────────────────────────────────────────

describe("FeatureGateNotice hard rules", () => {
  it.each([
    // Carrying plan fields and canUpgrade, to show the STATE decides.
    [
      "pending",
      gateOf({ state: "pending", planLabel: "XL", canUpgrade: true }),
    ],
    ["allowed", gateOf({ state: "allowed" })],
    ["allowed + degraded", gateOf({ state: "allowed", degraded: true })],
  ] as const)(
    "renders nothing at all for a %s gate — no skeleton, no placeholder",
    (_, gate) => {
      renderIn("en", <AllVariants gate={gate} scope="create" />);
      for (const variant of VARIANT_IDS) {
        expect(screen.getByTestId(variant)).toBeEmptyDOMElement();
      }
      expect(featureGateText(gate, gateT("en"))).toBeNull();
      expect(featureGateToast(gate, gateT("en"))).toBeNull();
    },
  );

  const ctaCases = REFUSED.flatMap((state) =>
    [true, false].map((canUpgrade) => [state, canUpgrade] as const),
  );

  it.each(ctaCases)(
    "state %s, canUpgrade %s: a CTA renders iff canUpgrade and the state is plan or quota",
    (state, canUpgrade) => {
      // Every field a CTA could need is present, so only the rule decides.
      const gate = gateOf({
        state,
        feature: "contexts",
        requiredPlan: "basic",
        planLabel: "M",
        currentPlan: "free",
        currentPlanLabel: "S",
        current: 1,
        limit: 1,
        requiredRole: WorkspaceRole.Admin,
        canUpgrade,
      });
      renderIn("en", <AllVariants gate={gate} />);

      const expectCta = canUpgrade && (state === "plan" || state === "quota");
      for (const variant of VARIANT_IDS) {
        const buttons = within(screen.getByTestId(variant)).queryAllByRole(
          "button",
        );
        expect(buttons).toHaveLength(expectCta ? 1 : 0);
      }
      expect(featureGateText(gate, gateT("en"))?.action !== null).toBe(
        expectCta,
      );
    },
  );

  it("deployment, role and allowlist have no action message in any combination", () => {
    for (const state of ["deployment", "role", "allowlist"] as const) {
      for (const scope of ["create", "all"] as const) {
        for (const role of [
          WorkspaceRole.Owner,
          WorkspaceRole.Admin,
          undefined,
        ] as const) {
          for (const bits of [0, 1, 2, 3, 4, 5, 6, 7]) {
            const keys = gateMessageKeys(
              state,
              scope,
              role,
              (bits & 1) !== 0,
              (bits & 2) !== 0,
              (bits & 4) !== 0,
            );
            expect(keys.action).toBeNull();
            expect(keys.upsell).toBeNull();
          }
        }
      }
    }
    // …and the catalogue has nowhere for one to come from.
    for (const locale of LOCALES) {
      const gate = CATALOGUES[locale].gate;
      expect(gate.deployment).not.toHaveProperty("action");
      expect(gate.role.owner).not.toHaveProperty("action");
      expect(gate.role.admin).not.toHaveProperty("action");
      expect(gate.allowlist).not.toHaveProperty("action");
    }
  });

  it.each(["inline", "page", "control"] as const)(
    "the %s CTA opens the Plan page",
    (variant) => {
      const gate = gateOf({
        state: "plan",
        requiredPlan: "promax",
        planLabel: "XL",
        canUpgrade: true,
      });
      renderIn("en", <FeatureGateNotice variant={variant} gate={gate} />);
      fireEvent.click(screen.getByRole("button", { name: "Upgrade to XL" }));
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
    },
  );

  it("defaults to scope all — the create-only 'existing ones keep working' copy is opt-in", () => {
    renderIn("en", <FeatureGateNotice gate={withTier} />);
    expect(
      screen.getByText("The XL plan includes resources"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/keep working/)).not.toBeInTheDocument();
    expect(featureGateToast(withTier, gateT("en"))?.title).toBe(
      "The XL plan includes resources",
    );
  });

  it("names tiers by their resolved label, never by the raw plan key", () => {
    const gate = gateOf({
      state: "quota",
      feature: "contexts",
      requiredPlan: "promax",
      planLabel: "Enterprise",
      currentPlan: "free",
      currentPlanLabel: "Starter",
      current: 1,
      limit: 1,
      canUpgrade: true,
    });
    renderIn("en", <FeatureGateNotice gate={gate} />);
    const text = document.body.textContent ?? "";
    expect(text).toContain("Your Starter plan");
    expect(text).toContain("The Enterprise plan raises this limit.");
    expect(text).not.toMatch(/promax|free/i);
  });

  it("passes only {feature}, {plan}, {currentPlan}, {current} and {limit}, each only when the gate carries it", () => {
    const allowed = new Set([
      "feature",
      "plan",
      "currentPlan",
      "current",
      "limit",
    ]);
    for (const { gate, scope } of BRANCHES) {
      const real = gateT("en");
      const calls: { key: string; values: Record<string, string | number> }[] =
        [];
      const recording: GateTranslator = (key, values) => {
        calls.push({ key, values: values ?? {} });
        return real(key, values);
      };
      featureGateText(gate, recording, scope);

      const messageCalls = calls.filter((c) => !c.key.startsWith("features."));
      expect(messageCalls.length).toBeGreaterThan(0);
      for (const { key, values } of messageCalls) {
        for (const name of Object.keys(values)) {
          expect(allowed, `${key} was passed {${name}}`).toContain(name);
        }
        expect(values.plan).toBe(gate.planLabel);
        expect(values.currentPlan).toBe(gate.currentPlanLabel);
        const hasNumbers =
          gate.current !== undefined && gate.limit !== undefined;
        expect(values.current).toBe(hasNumbers ? gate.current : undefined);
        expect(values.limit).toBe(hasNumbers ? gate.limit : undefined);
      }
    }
  });
});

// ── English grammar at any noun and any count ──────────────────────────────
//
// `{feature}` arrives as one pre-chosen string, so ICU cannot inflect it and
// nothing can make a verb agree with it: "Connectors is available", "1
// contexts". So no English message makes `{feature}` the subject of a verb
// or puts it beside a number — the noun is always an object ("The L plan
// includes connectors") or a modifier ("the context limit"), and the counts
// are read as usage, which suits a stock cap (contexts) and a daily one (API
// calls) alike.

describe("English copy reads grammatically for every noun and count", () => {
  const withPlan = (feature: GateKeyOf, current: number, limit: number) =>
    gateOf({
      state: "quota",
      feature,
      currentPlan: "free",
      currentPlanLabel: "S",
      current,
      limit,
    });
  const noPlan = (feature: GateKeyOf, current: number, limit: number) =>
    gateOf({ state: "quota", feature, current, limit });

  it.each([
    [
      "description, 0 of 1",
      withPlan("contexts", 0, 1),
      "Your S plan's context limit is 1, and current usage is 0.",
    ],
    [
      "description, 1 of 1",
      withPlan("contexts", 1, 1),
      "Your S plan's context limit is 1, and current usage is 1.",
    ],
    [
      "description, 2 of 5",
      withPlan("contexts", 2, 5),
      "Your S plan's context limit is 5, and current usage is 2.",
    ],
    [
      "descriptionNoPlan, 1 of 1",
      noPlan("contexts", 1, 1),
      "The context limit is 1, and current usage is 1.",
    ],
    [
      "descriptionNoPlan, 2 of 5",
      noPlan("contexts", 2, 5),
      "The context limit is 5, and current usage is 2.",
    ],
    [
      // The workspace cap is not a per-workspace quota: no "this workspace's
      // workspace limit".
      "descriptionNoPlan, the workspace cap",
      noPlan("workspaces", 1, 1),
      "The workspace limit is 1, and current usage is 1.",
    ],
    [
      // A daily cap is not "in use".
      "description, a daily cap",
      withPlan("api_calls", 2, 5),
      "Your S plan's API call limit is 5, and current usage is 2.",
    ],
  ])("quota %s", (_name, gate, expected) => {
    const text = featureGateText(gate, gateT("en"));
    expect(text?.description).toBe(expected);
  });

  it.each([
    ["contexts", "You've reached the context limit"],
    ["workspaces", "You've reached the workspace limit"],
    ["api_calls", "You've reached the API call limit"],
  ] as const)("quota title for %s", (feature, expected) => {
    expect(featureGateText(noPlan(feature, 1, 1), gateT("en"))?.title).toBe(
      expected,
    );
  });

  it("a plural noun and a singleton read the same way in every refusal", () => {
    const t = gateT("en");
    const connectors = (state: RefusedGateState, planLabel?: string) =>
      featureGateText(gateOf({ state, feature: "connectors", planLabel }), t);
    expect(connectors("plan", "XL")?.title).toBe(
      "The XL plan includes connectors",
    );
    expect(connectors("plan")?.title).toBe("No plan includes connectors");
    expect(connectors("plan")?.description).toBe(
      "No plan on this deployment includes connectors.",
    );
    expect(connectors("deployment")?.title).toBe(
      "This deployment does not offer connectors",
    );
    expect(connectors("deployment")?.description).toBe(
      "This Kagura Memory Cloud deployment has connectors turned off. Ask the administrator of this deployment if you need access.",
    );
    expect(connectors("allowlist")?.title).toBe(
      "This workspace does not have access to connectors yet",
    );
    expect(connectors("allowlist")?.description).toBe(
      "Access to connectors is being rolled out gradually. Reach out if you would like early access.",
    );
    expect(
      featureGateText(
        gateOf({ state: "deployment", feature: "secret_store" }),
        t,
      )?.title,
    ).toBe("This deployment does not offer the secret store");
  });

  // A gerund subject ("Managing {feature} is …") agrees with the gerund, so
  // only a sentence that STARTS with {feature} is caught.
  it("never starts a sentence with {feature} as its subject or puts it beside a number", () => {
    const leavesOf = (
      node: unknown,
      path: string[] = [],
    ): [string, string][] =>
      typeof node === "string"
        ? [[path.join("."), node]]
        : Object.entries(node as Messages).flatMap(([k, v]) =>
            leavesOf(v, [...path, k]),
          );
    const { features: _features, ...messages } = CATALOGUES.en.gate;
    const offending = leavesOf(messages).filter(
      ([, message]) =>
        /(^|[.!?] )\{feature\} (is|are|has|have|was|were)\b/.test(message) ||
        /\{(limit|current)\} \{feature\}/.test(message),
    );
    expect(offending).toEqual([]);
    // The Title-cased label only reads as a sentence subject.
    expect(Object.values(GATE_NOUN_FORM)).not.toContain("label");
  });
});

// ── Variant chrome ──────────────────────────────────────────────────────────

describe("FeatureGateNotice variants", () => {
  const deployment = gateOf({ state: "deployment", feature: "cost_dashboard" });

  it("page: with pageTitle and pageDescription it is the whole page (PageContainer + PageHeader)", () => {
    renderIn(
      "en",
      <FeatureGateNotice
        variant="page"
        gate={deployment}
        pageTitle="Cost"
        pageDescription="What this workspace spends"
      />,
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "Cost" }),
    ).toBeInTheDocument();
    expect(screen.getByText("What this workspace spends")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "This deployment does not offer the cost dashboard",
      }),
    ).toBeInTheDocument();
  });

  it("page: pageDescription is optional", () => {
    renderIn(
      "en",
      <FeatureGateNotice variant="page" gate={deployment} pageTitle="Cost" />,
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "Cost" }),
    ).toBeInTheDocument();
  });

  it("page: without pageTitle it is a bare EmptyState for use inside a page or tab", () => {
    renderIn("en", <FeatureGateNotice variant="page" gate={deployment} />);
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "This deployment does not offer the cost dashboard",
      }),
    ).toBeInTheDocument();
  });

  it("inline: uses the upsell Alert for plan, warning for quota and the default otherwise", () => {
    const cases = [
      [withTier, "bg-purple-50"],
      [gateOf({ state: "quota", feature: "contexts" }), "bg-amber-50"],
      [deployment, "bg-background"],
    ] as const;
    for (const [gate, token] of cases) {
      const { unmount } = renderIn("en", <FeatureGateNotice gate={gate} />);
      expect(screen.getByRole("alert")).toHaveClass(token);
      unmount();
    }
  });

  it("inline: id lands on the Alert so a control can aria-describedby it", () => {
    renderIn(
      "en",
      <>
        <button type="button" disabled aria-describedby="sharing-notice">
          Share
        </button>
        <FeatureGateNotice gate={withTier} scope="create" id="sharing-notice" />
      </>,
    );
    expect(screen.getByRole("alert")).toHaveAttribute("id", "sharing-notice");
    expect(
      screen.getByRole("button", { name: "Share" }),
    ).toHaveAccessibleDescription(/New resources require the XL plan/);
  });

  it("page: id lands on the notice body", () => {
    renderIn(
      "en",
      <FeatureGateNotice variant="page" gate={deployment} id="cost-notice" />,
    );
    expect(document.getElementById("cost-notice")).toHaveTextContent(
      "This deployment does not offer the cost dashboard",
    );
  });

  it("control: wraps the control, shows the badge, and exposes the hint by id", () => {
    const role = gateOf({
      state: "role",
      feature: "team_invitations",
      requiredRole: WorkspaceRole.Admin,
    });
    renderIn(
      "en",
      <FeatureGateNotice variant="control" gate={role} id="invite-gate">
        <button type="button" disabled aria-describedby="invite-gate">
          Invite
        </button>
      </FeatureGateNotice>,
    );
    const invite = screen.getByRole("button", { name: "Invite" });
    expect(invite).toHaveAccessibleDescription("Owner or admin only");
    // badge + hint
    expect(screen.getAllByText("Owner or admin only")).toHaveLength(2);
  });

  it("control: showBadge={false} keeps only the control and the hint", () => {
    const quota = gateOf({
      state: "quota",
      feature: "resource_tokens",
      current: 10,
      limit: 10,
    });
    renderIn(
      "en",
      <FeatureGateNotice variant="control" gate={quota} showBadge={false}>
        <span>Create token</span>
      </FeatureGateNotice>,
    );
    expect(screen.getByText("Create token")).toBeInTheDocument();
    expect(screen.getAllByText("Limit reached")).toHaveLength(1);
  });

  it("control: detail='description' renders the full sentence instead of the hint", () => {
    renderIn(
      "en",
      <FeatureGateNotice
        variant="control"
        gate={withTier}
        detail="description"
      />,
    );
    expect(
      screen.getByText("Upgrade to the XL plan to use resources."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Requires the XL plan")).not.toBeInTheDocument();
  });
});

// ── The toast ───────────────────────────────────────────────────────────────

describe("featureGateToast", () => {
  it("is destructive for quota only", () => {
    for (const state of REFUSED) {
      const toastArgs = featureGateToast(
        gateOf({ state, planLabel: "M", current: 1, limit: 1 }),
        gateT("en"),
      );
      expect(toastArgs?.variant).toBe(
        state === "quota" ? "destructive" : undefined,
      );
      expect(toastArgs).not.toHaveProperty("action");
    }
  });

  it("carries the same title and description as the rendered notice", () => {
    for (const { gate, scope } of BRANCHES) {
      const text = featureGateText(gate, gateT("ja"), scope);
      const toastArgs = featureGateToast(gate, gateT("ja"), scope);
      expect(toastArgs?.title).toBe(text?.title);
      expect(toastArgs?.description).toBe(text?.description);
    }
  });
});

// ── The noun-form table stays in step with the catalogues ──────────────────

describe("GATE_NOUN_FORM", () => {
  function leaves(node: unknown, path: string[] = []): [string, string][] {
    if (typeof node === "string") return [[path.join("."), node]];
    return Object.entries(node as Messages).flatMap(([k, v]) =>
      leaves(v, [...path, k]),
    );
  }

  it.each(LOCALES)(
    "covers every %s gate.* message and declares {feature} exactly where the message uses it",
    (locale) => {
      const { features: _features, ...messages } = CATALOGUES[locale].gate;
      const catalogue = leaves(messages);
      expect(Object.keys(GATE_NOUN_FORM).sort()).toEqual(
        catalogue.map(([key]) => key).sort(),
      );
      for (const [key, message] of catalogue) {
        const form = GATE_NOUN_FORM[key as keyof typeof GATE_NOUN_FORM];
        expect(form === null, key).toBe(!message.includes("{feature}"));
      }
    },
  );
});
