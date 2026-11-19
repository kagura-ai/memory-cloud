import { fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { WorkspaceSwitcher } from "./WorkspaceSwitcher";

const { switchWorkspace, workspaces } = vi.hoisted(() => ({
  switchWorkspace: vi.fn(),
  workspaces: [
    { id: "one", name: "Studio", current_user_role: "owner", plan_name: "free", context_count: 2, member_count: 1 },
    { id: "two", name: "Partners", current_user_role: "admin", plan_name: "free", context_count: 1, member_count: 3 },
  ],
}));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ user: { id: "u1", name: "Owner" } }) }));
vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({ currentWorkspace: workspaces[1], workspaces, loading: false, switchWorkspace }) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
describe("workspace switcher", () => {
  it.each([["ja", ja, "現在のワークスペース", "オーナー", "共有"], ["en", en, "Current workspace", "Owner", "Shared"]] as const)(
    "shows the active name and relationship before opening the %s switcher", (locale, messages, current, owned, shared) => {
      render(<NextIntlClientProvider locale={locale} messages={messages}><WorkspaceSwitcher /></NextIntlClientProvider>);
      const trigger = screen.getByRole("button", { name: new RegExp(current) });
      expect(trigger).toHaveTextContent("Partners");
      expect(trigger).toHaveTextContent(shared);
      fireEvent.click(trigger);
      const group = screen.getByRole("group", { name: messages.workspace.title });
      expect(within(group).getByRole("button", { name: /Studio/ })).toHaveTextContent(owned);
      expect(within(group).getByRole("button", { name: /Partners/ })).toHaveAttribute("aria-current", "true");
      fireEvent.keyDown(group, { key: "Escape" });
      expect(trigger).toHaveFocus();
      expect(trigger).toHaveAttribute("aria-expanded", "false");
    },
  );
});

describe("workspace switcher — suspended workspaces (#2919)", () => {
  const extra = [
    { id: "three", name: "Archive", current_user_role: "owner", plan_name: "free", context_count: 0, member_count: 1, slot_suspended: true },
    { id: "four", name: "Club", current_user_role: "member", plan_name: "free", context_count: 0, member_count: 5, slot_suspended: true },
  ];
  afterEach(() => {
    workspaces.splice(2);
    switchWorkspace.mockReset();
  });

  it.each([["ja", ja], ["en", en]] as const)(
    "%s: lists them labelled and disabled; the owner gets the page link, a member the ask-the-owner text",
    (locale, messages) => {
      workspaces.push(...extra);
      render(<NextIntlClientProvider locale={locale} messages={messages}><WorkspaceSwitcher /></NextIntlClientProvider>);
      fireEvent.click(screen.getByRole("button", { name: new RegExp(messages.workspace.currentWorkspaceCaption) }));
      const group = screen.getByRole("group", { name: messages.workspace.title });
      const archive = within(group).getByRole("button", { name: /Archive/ });
      const club = within(group).getByRole("button", { name: /Club/ });
      for (const button of [archive, club]) {
        expect(button).toBeDisabled();
        expect(button).toHaveTextContent(messages.workspaceSlots.card.suspended);
      }
      expect(within(group).getByRole("link", { name: messages.workspaceSlots.card.manage })).toHaveAttribute("href", "/profile/workspaces");
      expect(within(group).getAllByRole("link")).toHaveLength(1);
      expect(within(group).getByText(messages.workspaceSlots.card.askOwner)).toBeInTheDocument();
      fireEvent.click(archive);
      expect(switchWorkspace).not.toHaveBeenCalled();
    },
  );
});

