/**
 * The context page's Members tab (#2895): who may change members, what a
 * private context says instead of a list, and the privacy and access
 * explanation every role reads. Renders the real MembersSection; only the
 * API, auth, workspace and translations are mocked.
 */

import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { ContextMembersTabPanel } from "./ContextMembersTabPanel";
import type { Context } from "@/lib/types/context";
import type { ContextMember } from "@/lib/api/contexts";
import { ContextRole, WorkspaceRole } from "@/lib/auth/rbac";
import { ApiError } from "@/lib/api/base";

// ---------- Mocks ------------------------------------------------------------

const mockListContextMembers = vi.fn();
const mockAddContextMember = vi.fn();
const mockUpdateContextMemberRole = vi.fn();
const mockRemoveContextMember = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  listContextMembers: (...a: unknown[]) => mockListContextMembers(...a),
  addContextMember: (...a: unknown[]) => mockAddContextMember(...a),
  updateContextMemberRole: (...a: unknown[]) =>
    mockUpdateContextMemberRole(...a),
  removeContextMember: (...a: unknown[]) => mockRemoveContextMember(...a),
}));

const mockListWorkspaceMembers = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  listMembers: (...a: unknown[]) => mockListWorkspaceMembers(...a),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// Keys come back as `<namespace>.<key>` so a test can tell the panel's copy
// (contextDetail.membersTab.*) from the section's (contexts.*).
const translators = new Map<
  string,
  (k: string, v?: Record<string, unknown>) => string
>();
function translatorFor(ns: string) {
  let t = translators.get(ns);
  if (!t) {
    t = (k: string, v?: Record<string, unknown>) =>
      v && typeof v.name === "string" ? `${ns}.${k}:${v.name}` : `${ns}.${k}`;
    translators.set(ns, t);
  }
  return t;
}
vi.mock("next-intl", () => ({
  useTranslations: (ns: string) => translatorFor(ns),
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => mockUseAuth() }));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

// ---------- Helpers ----------------------------------------------------------

type Role = "owner" | "admin" | "member" | "viewer";
const ROLES: Role[] = ["owner", "admin", "member", "viewer"];

function makeContext(overrides: Partial<Context> = {}): Context {
  return {
    id: "ctx-1",
    name: "demo",
    display_name: "Demo",
    is_private: false,
    is_public: false,
    is_locked: false,
    is_default: false,
    created_at: "2026-04-01T00:00:00Z",
    updated_at: "2026-04-01T00:00:00Z",
    ...overrides,
  } as Context;
}

const SHARED = makeContext();
const PUBLIC = makeContext({ is_public: true });
const PRIVATE = makeContext({ is_private: true });

function member(overrides: Partial<ContextMember> = {}): ContextMember {
  return {
    user_id: "u-editor",
    user_name: "Editor",
    user_email: "editor@example.com",
    role: ContextRole.Editor,
    added_at: "2026-04-01T00:00:00Z",
    is_workspace_admin: false,
    ...overrides,
  };
}

function setRole(role: Role) {
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id: "ws-1", current_user_role: role },
    currentWorkspaceId: "ws-1",
  });
}

function renderPanel(context: Context, onOpenSettings = vi.fn()) {
  render(
    <ContextMembersTabPanel
      contextId={context.id}
      context={context}
      onOpenSettings={onOpenSettings}
    />,
  );
  return onOpenSettings;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue({ user: { id: "me" } });
  mockListContextMembers.mockResolvedValue([
    member(),
    member({
      user_id: "u-admin",
      user_email: "admin@example.com",
      role: WorkspaceRole.Admin,
      is_workspace_admin: true,
    }),
  ]);
  mockListWorkspaceMembers.mockResolvedValue([
    {
      user_id: "u-new",
      user_email: "new@example.com",
      user_name: "New",
      role: "member",
      joined_at: "2026-04-01T00:00:00Z",
    },
  ]);
  mockAddContextMember.mockResolvedValue({ access_list_extended: false });
  mockUpdateContextMemberRole.mockResolvedValue({});
  mockRemoveContextMember.mockResolvedValue(undefined);
});

// ---------- Shared / public: admin and owner manage --------------------------

describe("Members tab: admin/owner manage a shared or public context's members (#2895)", () => {
  it.each([
    ["owner", "shared", SHARED],
    ["admin", "shared", SHARED],
    ["owner", "public", PUBLIC],
    ["admin", "public", PUBLIC],
  ] as const)(
    "%s on a %s context can add, change and remove members",
    async (role, _kind, context) => {
      setRole(role);
      renderPanel(context);
      const row = await screen.findByTestId("member-row-u-editor");
      expect(screen.queryByTestId("members-readonly-notice")).toBeNull();

      // change
      fireEvent.change(within(row).getByRole("combobox"), {
        target: { value: "viewer" },
      });
      await waitFor(() =>
        expect(mockUpdateContextMemberRole).toHaveBeenCalledWith(
          "ctx-1",
          "u-editor",
          {
            role: "viewer",
          },
        ),
      );

      // remove
      fireEvent.click(
        within(row).getByRole("button", {
          name: "contexts.removeMember:editor@example.com",
        }),
      );
      fireEvent.click(
        await screen.findByRole("button", { name: "common.remove" }),
      );
      await waitFor(() =>
        expect(mockRemoveContextMember).toHaveBeenCalledWith(
          "ctx-1",
          "u-editor",
        ),
      );

      // add
      fireEvent.click(
        screen.getByRole("button", { name: "contexts.addMember" }),
      );
      const dialog = await screen.findByRole("dialog");
      const userSelect = await within(dialog).findByLabelText(
        "contexts.selectWorkspaceMember",
      );
      fireEvent.change(userSelect, { target: { value: "u-new" } });
      fireEvent.click(
        within(dialog).getByRole("button", { name: "contexts.addMember" }),
      );
      await waitFor(() =>
        expect(mockAddContextMember).toHaveBeenCalledWith("ctx-1", {
          user_id: "u-new",
          role: "editor",
        }),
      );
    },
  );
});

// ---------- Shared / public: member and viewer read --------------------------

describe("Members tab: member/viewer see the list read-only with a notice (#2895)", () => {
  it.each([
    ["member", "shared", SHARED],
    ["viewer", "shared", SHARED],
    ["member", "public", PUBLIC],
    ["viewer", "public", PUBLIC],
  ] as const)(
    "%s on a %s context sees names and roles but no write control",
    async (role, _kind, context) => {
      setRole(role);
      renderPanel(context);
      const row = await screen.findByTestId("member-row-u-editor");
      expect(within(row).getByText("editor@example.com")).toBeInTheDocument();
      expect(
        within(row).getByText("contexts.contextRoleEditor"),
      ).toBeInTheDocument();
      expect(screen.getByTestId("member-row-u-admin")).toBeInTheDocument();

      expect(screen.queryByRole("combobox")).toBeNull();
      expect(
        screen.queryByRole("button", { name: "contexts.addMember" }),
      ).toBeNull();
      expect(
        screen.queryByRole("button", { name: /^contexts\.removeMember/ }),
      ).toBeNull();

      expect(screen.getByTestId("members-readonly-notice")).toHaveTextContent(
        "contextDetail.membersTab.readOnlyNotice",
      );
      expect(mockListContextMembers).toHaveBeenCalledWith("ctx-1");
    },
  );

  it("an empty list for a viewer offers no add action", async () => {
    setRole("viewer");
    mockListContextMembers.mockResolvedValue([]);
    renderPanel(SHARED);
    expect(
      await screen.findByText("contexts.noMembersAssigned"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("contexts.noMembersReadOnlyHint"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "contexts.addMember" }),
    ).toBeNull();
  });

  it.each([
    ["member", 403],
    ["viewer", 403],
    ["member", 404],
    ["viewer", 404],
  ] as const)(
    "%s whose list load fails with %i sees the ErrorBanner, no table and no write control",
    async (role, status) => {
      setRole(role);
      mockListContextMembers.mockRejectedValue(
        new ApiError({
          status,
          message: "load failed",
          details: { detail: `members load failed (${status})` },
        }),
      );
      renderPanel(SHARED);
      expect(
        await screen.findByText(`members load failed (${status})`),
      ).toBeInTheDocument();
      expect(screen.queryByRole("table")).toBeNull();
      expect(screen.queryByText("contexts.noMembersAssigned")).toBeNull();
      expect(screen.queryByRole("combobox")).toBeNull();
      expect(
        screen.queryByRole("button", { name: "contexts.addMember" }),
      ).toBeNull();
      expect(screen.getByTestId("members-readonly-notice")).toBeInTheDocument();
    },
  );
});

// ---------- The "via workspace access" note ---------------------------------

describe("Members tab: the workspace-access note is for managers only (#2895)", () => {
  it.each(["owner", "admin"] as const)(
    "%s reads where workspace-access rows are managed",
    async (role) => {
      setRole(role);
      renderPanel(SHARED);
      expect(
        await screen.findByTestId("members-workspace-access-note"),
      ).toHaveTextContent("contexts.workspaceAccessNote");
    },
  );

  it.each(["member", "viewer"] as const)(
    "%s is not sent to a page where they cannot manage anything",
    async (role) => {
      setRole(role);
      renderPanel(SHARED);
      await screen.findByTestId("member-row-u-admin");
      expect(
        screen.getByText("contexts.viaWorkspaceAccess"),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("members-workspace-access-note")).toBeNull();
      expect(screen.queryByText("contexts.workspaceAccessNote")).toBeNull();
    },
  );
});

// ---------- Notices are notes, not alerts ------------------------------------

describe("Members tab: the static notices are not assertive alerts (#2895)", () => {
  it.each(["member", "viewer"] as const)(
    "%s: the read-only notice has role=note and nothing on the loaded tab is an alert",
    async (role) => {
      setRole(role);
      renderPanel(SHARED);
      await screen.findByTestId("member-row-u-editor");
      expect(screen.getByTestId("members-readonly-notice")).toHaveAttribute(
        "role",
        "note",
      );
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it.each(ROLES)(
    "%s: the private-context notice has role=note and is not an alert",
    (role) => {
      setRole(role);
      renderPanel(PRIVATE);
      expect(screen.getByTestId("members-private-notice")).toHaveAttribute(
        "role",
        "note",
      );
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );
});

// ---------- Private ----------------------------------------------------------

describe("Members tab: a private context shows a notice instead of a list (#2895)", () => {
  it.each(ROLES)(
    "%s sees the private-context notice and no list",
    async (role) => {
      setRole(role);
      renderPanel(PRIVATE);
      expect(screen.getByTestId("members-private-notice")).toHaveTextContent(
        "contextDetail.membersTab.privateNotice",
      );
      expect(screen.queryByText("contexts.contextMembers")).toBeNull();
      expect(screen.queryByRole("table")).toBeNull();
      expect(mockListContextMembers).not.toHaveBeenCalled();
    },
  );

  it.each(["owner", "admin"] as const)(
    "%s gets the button that switches to the Settings tab",
    (role) => {
      setRole(role);
      const onOpenSettings = renderPanel(PRIVATE);
      fireEvent.click(
        screen.getByRole("button", {
          name: "contextDetail.membersTab.openSettings",
        }),
      );
      expect(onOpenSettings).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["member", "viewer"] as const)(
    "%s gets no Settings button",
    (role) => {
      setRole(role);
      renderPanel(PRIVATE);
      expect(
        screen.queryByRole("button", {
          name: "contextDetail.membersTab.openSettings",
        }),
      ).toBeNull();
    },
  );
});

// ---------- Privacy and access explanation -----------------------------------

const EXPLANATION_KEYS = [
  "explanationTitle",
  "explainPrivate",
  "explainShared",
  "explainPublic",
  "explainRoles",
  "explainAccessListAdd",
  "explainAccessListWhere",
];

describe("Members tab: the privacy and access explanation (#2895)", () => {
  it.each(
    ROLES.flatMap((role) =>
      (
        [
          ["shared", SHARED],
          ["public", PUBLIC],
          ["private", PRIVATE],
        ] as const
      ).map(([kind, context]) => [role, kind, context] as const),
    ),
  )(
    "%s on a %s context reads the whole explanation",
    async (role, _kind, context) => {
      setRole(role);
      renderPanel(context);
      const explanation = screen.getByTestId("members-access-explanation");
      for (const key of EXPLANATION_KEYS) {
        expect(
          within(explanation).getByText(`contextDetail.membersTab.${key}`),
        ).toBeInTheDocument();
      }
      // Let the shared/public list load settle before the test ends.
      if (!context.is_private) {
        await screen.findByTestId("member-row-u-editor");
      }
    },
  );

  it.each(["owner", "admin"] as const)(
    "%s gets the link to the workspace Members page",
    (role) => {
      setRole(role);
      renderPanel(SHARED);
      const link = screen.getByRole("link", {
        name: "contextDetail.membersTab.workspaceMembersLink",
      });
      expect(link).toHaveAttribute("href", "/workspace/members");
    },
  );

  it.each(["member", "viewer"] as const)(
    "%s gets no link to the workspace Members page",
    (role) => {
      setRole(role);
      renderPanel(SHARED);
      expect(
        screen.queryByRole("link", {
          name: "contextDetail.membersTab.workspaceMembersLink",
        }),
      ).toBeNull();
    },
  );
});

// ---------- i18n -------------------------------------------------------------

describe("Members tab strings exist in both languages (#2895)", () => {
  type Catalogue = Record<string, Record<string, unknown>>;
  const pick = (cat: Catalogue, path: string): unknown =>
    path
      .split(".")
      .reduce<unknown>(
        (node, part) =>
          node && typeof node === "object"
            ? (node as Record<string, unknown>)[part]
            : undefined,
        cat,
      );
  const KEYS = [
    "contextDetail.tabs.members",
    ...[
      ...EXPLANATION_KEYS,
      "readOnlyNotice",
      "privateTitle",
      "privateNotice",
      "openSettings",
      "workspaceMembersLink",
    ].map((k) => `contextDetail.membersTab.${k}`),
    "contexts.noMembersReadOnlyHint",
    "contexts.visibilityMembersLink",
  ];

  it.each(KEYS)("%s is a non-empty string in en.json and ja.json", (key) => {
    for (const cat of [en, ja] as unknown as Catalogue[]) {
      const value = pick(cat, key);
      expect(typeof value).toBe("string");
      expect((value as string).trim()).not.toBe("");
    }
  });

  it("the ja Members tab copy spells memory メモリ like the tab bar", () => {
    const ja_ = ja as unknown as Catalogue;
    expect(pick(ja_, "contextDetail.tabs.memories")).toBe("メモリ");
    const tab = pick(ja_, "contextDetail.membersTab") as Record<string, string>;
    for (const value of Object.values(tab)) {
      expect(value).not.toContain("メモリー");
    }
  });
});
