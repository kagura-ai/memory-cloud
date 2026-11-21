/**
 * The notice on the plan page with the link back to the application (#2950).
 *
 * Pinned: it shows only with a valid marker, an operator-set https address and
 * the workspace's canonical owner; an arrival writes the marker once and takes
 * the parameter out of the address bar; the link is a plain same-tab anchor to
 * the configured address and nothing navigates by itself; "Check again" is one
 * quiet re-read. The wording itself is checked against the catalogues in
 * src/messages/applicationReturnCopy.test.ts (this translator echoes keys).
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Workspace } from "@/lib/api/workspaces";
import {
  APPLICATION_RETURN_STORAGE_KEY,
  PLAN_PAGE_PATH,
  rememberApplicationReturn,
  resetApplicationReturnFallback,
} from "@/lib/navigation/applicationReturn";

import { ApplicationReturnNotice } from "./ApplicationReturnNotice";

// Echo the key and the interpolated values, so a test can tell which message
// was chosen and what it was given without depending on the catalogue.
vi.mock("next-intl", () => ({
  useTranslations:
    () =>
    (key: string, values?: Record<string, string>): string =>
      values ? `${key} ${Object.values(values).join("|")}` : key,
}));

const mockPush = vi.fn();
const mockReplace = vi.fn();
let mockPathname = PLAN_PAGE_PATH;
let mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  usePathname: () => mockPathname,
  useSearchParams: () => mockSearchParams,
}));

interface MockUser {
  id: string;
  email: string;
  name: string;
}
const OWNER: MockUser = {
  id: "user-owner",
  email: "owner@example.com",
  name: "Owner Name",
};
let mockUser: MockUser | null = OWNER;
const mockRefetchUser = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockUser, refetchUser: mockRefetchUser }),
}));

const mockRefreshWorkspaces = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ refreshWorkspaces: mockRefreshWorkspaces }),
}));

const RETURN_URL = "https://app.example.com/return?step=plan";
let mockSystemInfo: { kagura_public_return_url?: string | null } | null = {
  kagura_public_return_url: RETURN_URL,
};
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => mockSystemInfo,
}));

function workspaceWith(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: "ws-1",
    name: "Research",
    description: null,
    owner_user_id: OWNER.id,
    plan_name: "free",
    member_count: 1,
    context_count: 0,
    created_at: "2026-11-01T00:00:00Z",
    current_user_role: "owner",
    ...overrides,
  };
}

const onRechecked = vi.fn();

function renderNotice(workspace: Workspace = workspaceWith()) {
  return render(
    <ApplicationReturnNotice
      workspace={workspace}
      planLabel="Max"
      onRechecked={onRechecked}
    />,
  );
}

const TITLE = "planPage.applicationReturn.title";

function storedMarkerWrites(setItem: { mock: { calls: unknown[][] } }): number {
  return setItem.mock.calls.filter(
    ([key]) => key === APPLICATION_RETURN_STORAGE_KEY,
  ).length;
}

beforeEach(() => {
  window.sessionStorage.clear();
  resetApplicationReturnFallback();
  mockPush.mockReset();
  mockReplace.mockReset();
  mockRefetchUser.mockReset().mockImplementation(async () => mockUser);
  mockRefreshWorkspaces.mockReset().mockResolvedValue(undefined);
  onRechecked.mockReset();
  mockPathname = PLAN_PAGE_PATH;
  mockSearchParams = new URLSearchParams();
  mockUser = OWNER;
  mockSystemInfo = { kagura_public_return_url: RETURN_URL };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  resetApplicationReturnFallback();
});

describe("ApplicationReturnNotice (#2950)", () => {
  it("renders nothing without a marker", () => {
    const { container } = renderNotice();
    expect(container).toBeEmptyDOMElement();
    expect(mockReplace).not.toHaveBeenCalled();
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).toBeNull();
  });

  it.each([
    ["while system info loads", null],
    ["when the deployment sets no address", { kagura_public_return_url: null }],
    ["on an older backend without the field", {}],
  ])(
    "renders and writes nothing when no address is set (%s)",
    (_label, info) => {
      mockSystemInfo = info;
      mockSearchParams = new URLSearchParams("return=kagura-public");
      const { container } = renderNotice();
      expect(container).toBeEmptyDOMElement();
      expect(
        window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
      ).toBeNull();
      // The parameter stays in the address bar.
      expect(mockReplace).not.toHaveBeenCalled();
      expect(mockPush).not.toHaveBeenCalled();
    },
  );

  it.each([
    "javascript:alert(1)",
    "http://app.example.com/return",
    "https://user:pass@app.example.com/return",
    "app.example.com/return",
  ])("renders nothing for an unsafe address (%s)", (address) => {
    mockSystemInfo = { kagura_public_return_url: address };
    mockSearchParams = new URLSearchParams("return=kagura-public");
    const { container } = renderNotice();
    expect(container).toBeEmptyDOMElement();
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it.each([
    [
      "an owner-role member who is not the owner",
      { owner_user_id: "user-other" },
      OWNER,
    ],
    [
      "the owner id without the owner role",
      { current_user_role: "admin" },
      OWNER,
    ],
    ["no role", { current_user_role: null }, OWNER],
    ["no signed-in user", {}, null],
  ])(
    "renders nothing for a non-canonical owner (%s)",
    (_label, overrides, user) => {
      mockUser = user;
      mockSearchParams = new URLSearchParams("return=kagura-public");
      const first = renderNotice(workspaceWith(overrides));
      expect(first.container).toBeEmptyDOMElement();
      expect(
        window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
      ).toBeNull();
      expect(mockReplace).not.toHaveBeenCalled();
      first.unmount();

      // A marker from an earlier arrival does not show it either.
      rememberApplicationReturn();
      mockSearchParams = new URLSearchParams();
      const second = renderNotice(workspaceWith(overrides));
      expect(second.container).toBeEmptyDOMElement();
    },
  );

  it("an arrival writes the marker once, removes the parameter and shows the notice", async () => {
    const setItem = vi.spyOn(window.sessionStorage, "setItem");
    mockSearchParams = new URLSearchParams("return=kagura-public");
    renderNotice();
    expect(
      await screen.findByRole("region", { name: TITLE }),
    ).toBeInTheDocument();
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).toMatch(/^\d{1,16}$/);
    expect(storedMarkerWrites(setItem)).toBe(1);
    expect(mockReplace).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledWith(PLAN_PAGE_PATH, { scroll: false });
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("does not write again when the search params change", async () => {
    const setItem = vi.spyOn(window.sessionStorage, "setItem");
    mockSearchParams = new URLSearchParams("return=kagura-public");
    const { rerender } = renderNotice();
    await screen.findByRole("region", { name: TITLE });

    // A new params object with the same arrival, then the cleaned address.
    mockSearchParams = new URLSearchParams("return=kagura-public");
    rerender(
      <ApplicationReturnNotice
        workspace={workspaceWith()}
        planLabel="Max"
        onRechecked={onRechecked}
      />,
    );
    mockSearchParams = new URLSearchParams();
    rerender(
      <ApplicationReturnNotice
        workspace={workspaceWith()}
        planLabel="Max"
        onRechecked={onRechecked}
      />,
    );

    expect(screen.getByRole("region", { name: TITLE })).toBeInTheDocument();
    expect(storedMarkerWrites(setItem)).toBe(1);
    expect(mockReplace).toHaveBeenCalledTimes(1);
  });

  it("a load with ?session_id=…&changed=… writes nothing", async () => {
    mockSearchParams = new URLSearchParams("session_id=cs_test_123&changed=1");
    const first = renderNotice();
    expect(first.container).toBeEmptyDOMElement();
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();
    first.unmount();

    // Back from the payment service with a marker: shown, nothing rewritten.
    rememberApplicationReturn();
    const marker = window.sessionStorage.getItem(
      APPLICATION_RETURN_STORAGE_KEY,
    );
    const setItem = vi.spyOn(window.sessionStorage, "setItem");
    renderNotice();
    expect(
      await screen.findByRole("region", { name: TITLE }),
    ).toBeInTheDocument();
    expect(storedMarkerWrites(setItem)).toBe(0);
    expect(window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY)).toBe(
      marker,
    );
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("before the purchase: not-on-plan text, Check again, a text link", () => {
    rememberApplicationReturn();
    renderNotice(workspaceWith({ plan_name: "pro" }));
    expect(
      screen.getByText("planPage.applicationReturn.notOnPlan Research|Max"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/planPage\.applicationReturn\.onPlan/),
    ).toBeNull();
    expect(
      screen.getByText("planPage.applicationReturn.switchHint"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "planPage.applicationReturn.checkAgain",
      }),
    ).toBeInTheDocument();
    const link = screen.getByRole("link", {
      name: "planPage.applicationReturn.returnLink",
    });
    expect(link).toHaveAttribute("href", RETURN_URL);
    // A text link here, not the primary button.
    expect(link).not.toHaveClass("bg-primary");
    expect(
      screen.getByRole("button", {
        name: "planPage.applicationReturn.dismiss",
      }),
    ).toBeInTheDocument();
  });

  it("after: on-plan text and the primary link", () => {
    rememberApplicationReturn();
    renderNotice(workspaceWith({ plan_name: "promax" }));
    expect(
      screen.getByText("planPage.applicationReturn.onPlan Research|Max"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/planPage\.applicationReturn\.notOnPlan/),
    ).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "planPage.applicationReturn.checkAgain",
      }),
    ).toBeNull();
    const link = screen.getByRole("link", {
      name: "planPage.applicationReturn.returnLink",
    });
    expect(link).toHaveAttribute("href", RETURN_URL);
    expect(link).toHaveClass("bg-primary");
    expect(
      screen.getByRole("button", {
        name: "planPage.applicationReturn.dismiss",
      }),
    ).toBeInTheDocument();
  });

  it("shows the email, then the name, never the id", () => {
    rememberApplicationReturn();
    const first = renderNotice();
    const signedInAs = screen.getByText(
      `planPage.applicationReturn.signedInAs ${OWNER.email}`,
    );
    // The first line of the notice.
    expect(signedInAs.parentElement?.firstElementChild).toBe(signedInAs);
    expect(document.body.textContent ?? "").not.toMatch(/user-owner/);
    first.unmount();

    mockUser = { ...OWNER, email: "" };
    const second = renderNotice();
    expect(
      screen.getByText(`planPage.applicationReturn.signedInAs ${OWNER.name}`),
    ).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/user-owner/);
    second.unmount();

    mockUser = { ...OWNER, email: "", name: "" };
    renderNotice();
    expect(screen.getByRole("region", { name: TITLE })).toBeInTheDocument();
    expect(
      screen.queryByText(/planPage\.applicationReturn\.signedInAs/),
    ).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/user-owner/);
  });

  it("the link is a same-tab anchor to the configured address; nothing navigates by itself", async () => {
    const before = window.location.href;
    mockSearchParams = new URLSearchParams(
      "return=kagura-public&next=https%3A%2F%2Fother.example.org%2F&to=%2Fadmin",
    );
    renderNotice(workspaceWith({ plan_name: "promax" }));
    const link = await screen.findByRole("link", {
      name: "planPage.applicationReturn.returnLink",
    });
    expect(link.tagName).toBe("A");
    // Exactly the configured address: nothing taken from the URL, nothing appended.
    expect(link.getAttribute("href")).toBe(RETURN_URL);
    expect(link).not.toHaveAttribute("target");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(screen.getAllByRole("link")).toHaveLength(1);

    // The only navigation is the replace that drops the parameter.
    await waitFor(() => expect(mockReplace).toHaveBeenCalledTimes(1));
    expect(mockReplace).toHaveBeenCalledWith(PLAN_PAGE_PATH, { scroll: false });
    expect(mockPush).not.toHaveBeenCalled();
    expect(window.location.href).toBe(before);
  });

  it("Check again re-reads the user and the workspaces quietly, calls onRechecked and announces the plan", async () => {
    rememberApplicationReturn();
    const { rerender } = renderNotice();
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("");
    expect(status).toHaveClass("sr-only");

    fireEvent.click(
      screen.getByRole("button", {
        name: "planPage.applicationReturn.checkAgain",
      }),
    );
    await waitFor(() => expect(onRechecked).toHaveBeenCalledTimes(1));
    expect(mockRefetchUser).toHaveBeenCalledTimes(1);
    expect(mockRefreshWorkspaces).toHaveBeenCalledTimes(1);
    expect(mockRefreshWorkspaces).toHaveBeenCalledWith({ quiet: true });
    expect(mockRefetchUser.mock.invocationCallOrder[0]).toBeLessThan(
      mockRefreshWorkspaces.mock.invocationCallOrder[0],
    );
    expect(mockRefreshWorkspaces.mock.invocationCallOrder[0]).toBeLessThan(
      onRechecked.mock.invocationCallOrder[0],
    );
    // No polling: nothing is read again by itself.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(mockRefetchUser).toHaveBeenCalledTimes(1);
    expect(mockRefreshWorkspaces).toHaveBeenCalledTimes(1);

    // The re-read workspace list now says the top plan.
    rerender(
      <ApplicationReturnNotice
        workspace={workspaceWith({ plan_name: "promax" })}
        planLabel="Max"
        onRechecked={onRechecked}
      />,
    );
    const announced = await screen.findByText(
      "planPage.applicationReturn.nowOnPlan Max",
    );
    expect(announced).toHaveAttribute("role", "status");
    expect(announced).toHaveAttribute("aria-live", "polite");
    expect(
      screen.getByText("planPage.applicationReturn.onPlan Research|Max"),
    ).toBeInTheDocument();
  });

  it("announces nothing when the top plan was there before any re-check", () => {
    rememberApplicationReturn();
    renderNotice(workspaceWith({ plan_name: "promax" }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("hides itself when Check again finds another account", async () => {
    rememberApplicationReturn();
    const other: MockUser = {
      id: "user-second",
      email: "second@example.com",
      name: "Second",
    };
    mockRefetchUser.mockImplementation(async () => {
      mockUser = other;
      return other;
    });
    renderNotice();
    fireEvent.click(
      screen.getByRole("button", {
        name: "planPage.applicationReturn.checkAgain",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: TITLE })).toBeNull(),
    );
    // The marker stays: the notice shows again for the owner's own account.
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).not.toBeNull();
  });

  it("Hide this notice removes the marker", async () => {
    rememberApplicationReturn();
    renderNotice();
    fireEvent.click(
      screen.getByRole("button", {
        name: "planPage.applicationReturn.dismiss",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: TITLE })).toBeNull(),
    );
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).toBeNull();
    expect(mockPush).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("the notice is a labelled region, not an alert", () => {
    rememberApplicationReturn();
    renderNotice();
    const region = screen.getByRole("region", { name: TITLE });
    const labelledBy = region.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy ?? "")).toHaveTextContent(TITLE);
    expect(screen.queryByRole("alert")).toBeNull();
    // The polite status sits inside the region, not inside an alert.
    expect(region).toContainElement(screen.getByRole("status"));
  });
});
