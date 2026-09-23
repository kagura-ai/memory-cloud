/**
 * ContextPrivacyChoice (#1646): the Private / Shared pair both create dialogs
 * on the contexts page render.
 *
 * Rendered with the REAL en / ja catalogues (the FeatureGateNotice.test.tsx
 * pattern), because the shared option's refusal is `gate.*` copy formatted
 * from the descriptor: a key-echo mock cannot see a tier name leak back in,
 * or a message formatted without an argument it needs. `afterEach` fails the
 * test on any error the provider reports.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContextPrivacyChoice } from "./ContextPrivacyChoice";
import type { FeatureGate } from "@/lib/gates/featureGates";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const mockPush = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

const CATALOGUES = { en, ja } as const;
type Locale = keyof typeof CATALOGUES;

let intlErrors: string[] = [];

beforeEach(() => {
  intlErrors = [];
  mockPush.mockReset();
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

function sharedGate(g: Partial<FeatureGate> & Pick<FeatureGate, "state">) {
  return { feature: "shared_contexts", canUpgrade: false, ...g } as FeatureGate;
}

const ALLOWED = sharedGate({ state: "allowed" });
const PENDING = sharedGate({ state: "pending" });
/** A tier without shared contexts, the L tier lifting it, an owner who can act. */
const REFUSED = sharedGate({
  state: "plan",
  requiredPlan: "pro",
  planLabel: "L",
  currentPlan: "basic",
  currentPlanLabel: "M",
  canUpgrade: true,
});

function renderChoice(
  props: Partial<Parameters<typeof ContextPrivacyChoice>[0]> = {},
  locale: Locale = "en",
) {
  const onChange = vi.fn();
  renderIn(
    locale,
    <ContextPrivacyChoice
      isPrivate
      onChange={onChange}
      isAdmin={false}
      shared={ALLOWED}
      dialog="advanced"
      {...props}
    />,
  );
  return {
    onChange,
    privateRadio: document.querySelector(
      'input[type="radio"][value="private"]',
    ) as HTMLInputElement,
    sharedRadio: document.querySelector(
      'input[type="radio"][value="shared"]',
    ) as HTMLInputElement,
  };
}

describe("ContextPrivacyChoice — the shared option", () => {
  it("allowed: the radio works and says who can access", () => {
    const { sharedRadio, onChange } = renderChoice({ shared: ALLOWED });

    expect(sharedRadio).not.toBeDisabled();
    expect(sharedRadio).toHaveAccessibleName("Shared");
    expect(
      screen.getByText("Team members can access based on their roles."),
    ).toBeInTheDocument();
    fireEvent.click(sharedRadio);
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("pending: inert and silent — no upsell before the tier is known", () => {
    const { sharedRadio, onChange } = renderChoice({ shared: PENDING });

    expect(sharedRadio).toBeDisabled();
    expect(sharedRadio).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText(/Team members can access/)).toBeNull();
    expect(screen.queryByText(/Upgrade|Requires| plan$/)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    fireEvent.click(sharedRadio);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refused on this tier: the gate's badge and sentence, named by the resolved label, described to the radio", () => {
    const { sharedRadio, onChange } = renderChoice({ shared: REFUSED });

    expect(sharedRadio).toBeDisabled();
    expect(sharedRadio).toHaveAccessibleName("Shared");
    expect(sharedRadio).toHaveAccessibleDescription(
      "Upgrade to the L plan to use shared contexts.",
    );
    expect(screen.getByText("L plan")).toBeInTheDocument();
    expect(screen.queryByText(/Team members can access/)).toBeNull();
    // No tier word from the old contexts.* copy ("Pro Plan", "Pro").
    expect(document.body.textContent).not.toMatch(/\bpro\b/i);
    fireEvent.click(sharedRadio);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refused, and this member may upgrade: the CTA opens the Plan page", () => {
    renderChoice({ shared: REFUSED });

    // Named by its own text: inside a <label> it would take the whole card's.
    fireEvent.click(screen.getByRole("button", { name: "Upgrade to L" }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it("refused, but this member cannot upgrade here: the explanation stays, the CTA does not", () => {
    renderChoice({ shared: { ...REFUSED, canUpgrade: false } });

    expect(
      screen.getByText("Upgrade to the L plan to use shared contexts."),
    ).toBeInTheDocument();
    expect(screen.getByText("L plan")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("refused and no served tier has shared contexts: says so, with no tier, badge or CTA", () => {
    const { sharedRadio } = renderChoice({
      shared: sharedGate({ state: "plan", canUpgrade: false }),
    });

    expect(sharedRadio).toHaveAccessibleDescription(
      "No plan on this deployment includes shared contexts.",
    );
    expect(screen.queryByText(/ plan$/)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders the same refusal in Japanese", () => {
    const { sharedRadio } = renderChoice({ shared: REFUSED }, "ja");

    expect(sharedRadio).toHaveAccessibleName("共有");
    expect(sharedRadio).toHaveAccessibleDescription(
      "共有コンテキスト を利用するには L プランにアップグレードしてください。",
    );
    expect(screen.getByText("L プラン")).toBeInTheDocument();
  });

  it("both dialogs render the shared option identically", () => {
    const advanced = renderChoice({ shared: REFUSED, dialog: "advanced" })
      .sharedRadio.parentElement?.textContent;
    cleanup();
    const quick = renderChoice({ shared: REFUSED, dialog: "quick" }).sharedRadio
      .parentElement?.textContent;

    expect(advanced).toBeTruthy();
    expect(quick).toBe(advanced);
  });
});

describe("ContextPrivacyChoice — the private option", () => {
  // The private option is a role rule, not a plan gate: its copy is the
  // contexts.* helper each dialog has always shown (P-17).
  it.each([
    [
      "advanced",
      false,
      "Only you can access this context. Available on all plans.",
    ],
    [
      "advanced",
      true,
      "Only owners can create private contexts. Admins can create shared contexts.",
    ],
    ["quick", false, "Only you can access"],
    ["quick", true, "Admins can only create shared"],
  ] as const)("%s dialog, admin %s: %s", (dialog, isAdmin, helper) => {
    const { privateRadio } = renderChoice({ dialog, isAdmin });

    expect(screen.getByText(helper)).toBeInTheDocument();
    expect(privateRadio.disabled).toBe(isAdmin);
    expect(screen.queryByText("Owner only") !== null).toBe(isAdmin);
  });

  it("an owner picks private", () => {
    const { privateRadio, onChange } = renderChoice({
      isPrivate: false,
      shared: ALLOWED,
    });

    fireEvent.click(privateRadio);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("an admin cannot pick private", () => {
    const { privateRadio, onChange } = renderChoice({
      isPrivate: false,
      isAdmin: true,
      shared: ALLOWED,
    });

    fireEvent.click(privateRadio);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("marks the selected option", () => {
    const { privateRadio, sharedRadio } = renderChoice({
      isPrivate: false,
      shared: ALLOWED,
    });

    expect(sharedRadio).toBeChecked();
    expect(privateRadio).not.toBeChecked();
  });
});
