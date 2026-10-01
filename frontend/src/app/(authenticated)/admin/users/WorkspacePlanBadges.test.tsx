/**
 * Tests for the workspace + plan badges on the admin user list (#1754).
 *
 * Covers the plan label with and without deployment display names (both the
 * per-locale JSON map and the single-string env), the neutral chip for a tier
 * the client does not know, the translated role in the badge title, and the
 * "+N" overflow tooltip that lists the hidden workspaces with their plans.
 *
 * Radix Tooltip opens on focus as well as hover, so the tests focus the
 * trigger instead of simulating the pointer dance jsdom does not support; the
 * tap/click cases send the real pointerdown → click order.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Echo keys; `roles.*` resolves the two roles the tests use so the title
// shows a translated role and falls back to the raw string otherwise.
const ROLE_LABELS: Record<string, string> = {
  owner: "Owner",
  member: "Member",
};
vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => {
    const translate = (key: string, params?: Record<string, unknown>) => {
      if (namespace.endsWith(".roles")) return ROLE_LABELS[key] ?? key;
      if (key === "workspaceTooltip" && params) {
        return `${params.name} · ${params.role} · ${params.plan}`;
      }
      if (!params) return key;
      const tail = Object.entries(params)
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");
      return `${key} ${tail}`;
    };
    translate.has = (key: string) =>
      namespace.endsWith(".roles") ? key in ROLE_LABELS : true;
    return translate;
  },
  useLocale: () => "en",
}));
// PlanBadge reads the locale from the app's own provider, not next-intl.
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: "en" }) }));

import { WorkspacePlanBadges } from "./WorkspacePlanBadges";
import { WORKSPACE_BADGE_TEST_IDS } from "./testids";

const ws = (id: string, name: string, plan: string, role = "member") => ({
  workspace_id: id,
  workspace_name: name,
  role,
  plan_name: plan,
});

const THREE = [
  ws("w1", "One", "free"),
  ws("w2", "Two", "basic"),
  ws("w3", "Three", "pro"),
];

const ENV_KEYS = [
  "NEXT_PUBLIC_PLAN_DISPLAY_NAMES",
  "NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME",
] as const;

afterEach(() => {
  for (const k of ENV_KEYS) delete process.env[k];
});

/** What a mouse click or a tap sends: pointerdown, then click. */
const tap = (el: HTMLElement) => {
  fireEvent.pointerDown(el, { button: 0, pointerType: "mouse" });
  fireEvent.click(el);
};

/** Text of the nested plan chip: the badge's last child element. */
const planChipOf = (badge: HTMLElement) =>
  (badge.lastElementChild as HTMLElement).textContent;

describe("WorkspacePlanBadges", () => {
  it("shows each visible workspace with its plan label (OSS default)", () => {
    render(
      <WorkspacePlanBadges
        workspaces={[
          ws("w1", "Personal", "pro", "owner"),
          ws("w2", "Team", "free"),
        ]}
      />,
    );
    const badges = screen.getAllByTestId(WORKSPACE_BADGE_TEST_IDS.badge);
    expect(badges).toHaveLength(2);
    expect(badges[0]).toHaveTextContent("Personal");
    expect(planChipOf(badges[0])).toBe("L");
    expect(badges[1]).toHaveTextContent("Team");
    expect(planChipOf(badges[1])).toBe("S");
    expect(
      screen.queryByTestId(WORKSPACE_BADGE_TEST_IDS.overflow),
    ).not.toBeInTheDocument();
  });

  it("uses the deployment's per-locale plan display names when configured", () => {
    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      en: { pro: "Max" },
    });
    render(<WorkspacePlanBadges workspaces={[ws("w1", "Personal", "pro")]} />);
    expect(planChipOf(screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.badge))).toBe(
      "Max",
    );
  });

  it("uses the single-string plan display name env when configured", () => {
    process.env.NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME = "Business";
    render(<WorkspacePlanBadges workspaces={[ws("w1", "Personal", "pro")]} />);
    expect(planChipOf(screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.badge))).toBe(
      "Business",
    );
  });

  it("renders a tier the client does not know as its raw name (defensive)", () => {
    render(
      <WorkspacePlanBadges workspaces={[ws("w1", "Lab", "enterprise")]} />,
    );
    const badge = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.badge);
    expect(badge).toHaveTextContent("Lab");
    expect(planChipOf(badge)).toBe("enterprise");
  });

  it("titles a visible badge with name · translated role · plan label, without making it a tab stop", () => {
    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      en: { pro: "Max" },
    });
    render(
      <WorkspacePlanBadges
        workspaces={[ws("w1", "Personal", "pro", "owner")]}
      />,
    );
    const badge = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.badge);
    expect(badge).toHaveAttribute("title", "Personal · Owner · Max");
    expect(badge).not.toHaveAttribute("tabindex");
  });

  it("shows a role with no translation as the raw string", () => {
    render(
      <WorkspacePlanBadges workspaces={[ws("w1", "Lab", "free", "viewer")]} />,
    );
    expect(screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.badge)).toHaveAttribute(
      "title",
      "Lab · viewer · S",
    );
  });

  it("collapses the third and later workspaces into a focusable +N trigger that lists them with plans", async () => {
    render(
      <WorkspacePlanBadges
        workspaces={[
          ws("w1", "One", "free"),
          ws("w2", "Two", "basic"),
          ws("w3", "Three", "pro", "owner"),
          ws("w4", "Four", "promax"),
        ]}
      />,
    );
    expect(screen.getAllByTestId(WORKSPACE_BADGE_TEST_IDS.badge)).toHaveLength(
      2,
    );

    const overflow = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.overflow);
    expect(overflow.tagName).toBe("BUTTON");
    expect(overflow).toHaveTextContent("+2");
    expect(overflow).toHaveAttribute("aria-label", "moreWorkspaces count=2");

    fireEvent.focus(overflow);
    const tooltip = await screen.findByRole("tooltip");
    const items = within(tooltip)
      .getAllByRole("listitem")
      .map((li) => li.textContent);
    expect(items).toEqual(["Three · Owner · L", "Four · Member · XL"]);
  });

  it("toggles the overflow list on tap without bubbling to the row", async () => {
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <WorkspacePlanBadges workspaces={THREE} />
      </div>,
    );
    const overflow = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.overflow);
    tap(overflow);
    expect(onRowClick).not.toHaveBeenCalled();
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Three");
    // The tooltip body renders in a portal, but React events still bubble
    // through the component tree to the row.
    fireEvent.click(within(tooltip).getByText("Three · Member · L"));
    expect(onRowClick).not.toHaveBeenCalled();
    tap(overflow);
    expect(overflow).toHaveAttribute("data-state", "closed");
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("keeps the list open when focus opens it between pointerup and click (touch order)", () => {
    render(<WorkspacePlanBadges workspaces={THREE} />);
    const overflow = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.overflow);
    fireEvent.pointerDown(overflow, { button: 0, pointerType: "touch" });
    fireEvent.pointerUp(overflow, { button: 0, pointerType: "touch" });
    fireEvent.focus(overflow);
    fireEvent.click(overflow);
    expect(overflow).not.toHaveAttribute("data-state", "closed");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Three");
  });

  it("closes a tapped-open overflow list on Escape and on an outside press", async () => {
    render(<WorkspacePlanBadges workspaces={THREE} />);
    const overflow = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.overflow);

    tap(overflow);
    await screen.findByRole("tooltip");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(overflow).toHaveAttribute("data-state", "closed");

    tap(overflow);
    await screen.findByRole("tooltip");
    fireEvent.pointerDown(document.body);
    expect(overflow).toHaveAttribute("data-state", "closed");
  });

  it("closes a hover-opened overflow list on click instead of flickering it back open", async () => {
    render(<WorkspacePlanBadges workspaces={THREE} />);
    const overflow = screen.getByTestId(WORKSPACE_BADGE_TEST_IDS.overflow);

    fireEvent.focus(overflow);
    await screen.findByRole("tooltip");
    tap(overflow);
    expect(overflow).toHaveAttribute("data-state", "closed");
  });
});
