import { fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
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
