/**
 * Tests for ConnectedAppsSection (#2129): the OAuth apps the user authorized,
 * each with its MCP default context (PUT to set, DELETE to clear).
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectedAppsSection } from "./ConnectedAppsSection";
import type { OAuthGrant } from "@/lib/types/oauth-grant";

const mockList = vi.fn();
const mockSet = vi.fn();
const mockClear = vi.fn();
vi.mock("@/lib/api/oauth-grants", () => ({
  listOAuthGrants: (...a: unknown[]) => mockList(...a),
  setOAuthGrantDefaultContext: (...a: unknown[]) => mockSet(...a),
  clearOAuthGrantDefaultContext: (...a: unknown[]) => mockClear(...a),
}));

const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspaceId: "ws-1" }),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1", timezone: "UTC" } }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const CTX_A = "11111111-1111-4111-8111-111111111111";
const CTX_B = "22222222-2222-4222-8222-222222222222";

function grant(overrides: Partial<OAuthGrant> = {}): OAuthGrant {
  return {
    client_id: "client-claude",
    client_name: "Claude",
    scopes: ["memory:read", "memory:write"],
    default_context_id: null,
    default_context_name: null,
    last_issued_at: "2026-10-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetContexts.mockResolvedValue({
    contexts: [
      { id: CTX_A, name: "alpha", display_name: "Alpha" },
      { id: CTX_B, name: "beta", display_name: null },
    ],
    total: 2,
  });
});

describe("ConnectedAppsSection (#2129)", () => {
  it("shows the empty state when no app is authorized", async () => {
    mockList.mockResolvedValue([]);
    render(<ConnectedAppsSection />);
    expect(await screen.findByText("emptyTitle")).toBeInTheDocument();
    expect(screen.getByText("emptyDescription")).toBeInTheDocument();
  });

  it("shows a load failure as a banner, not the empty state", async () => {
    mockList.mockRejectedValue(new Error("Network down"));
    render(<ConnectedAppsSection />);
    expect(await screen.findByText("Network down")).toBeInTheDocument();
    expect(screen.queryByText("emptyTitle")).not.toBeInTheDocument();
  });

  it("falls back to the translated message for an empty error", async () => {
    mockList.mockRejectedValue(new Error(""));
    render(<ConnectedAppsSection />);
    expect(await screen.findByText("loadError")).toBeInTheDocument();
  });

  it("lists each app with its scopes, last authorization and default", async () => {
    mockList.mockResolvedValue([
      grant({ default_context_id: CTX_A, default_context_name: "alpha" }),
      grant({
        client_id: "client-cursor",
        client_name: "Cursor",
        scopes: [],
        last_issued_at: null,
      }),
    ]);
    render(<ConnectedAppsSection />);

    const list = await screen.findByRole("list");
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Claude")).toBeInTheDocument();
    expect(within(rows[0]).getByText("memory:read")).toBeInTheDocument();
    expect(within(rows[0]).getByText("memory:write")).toBeInTheDocument();
    const select = within(rows[0]).getByLabelText(
      "defaultContextFor",
    ) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(CTX_A));

    expect(within(rows[1]).getByText("Cursor")).toBeInTheDocument();
    // No scopes and no live token → em-dashes, and no default set.
    expect(within(rows[1]).getAllByText("—")).toHaveLength(2);
    expect(
      (within(rows[1]).getByLabelText("defaultContextFor") as HTMLSelectElement)
        .value,
    ).toBe("");
    // Loaded once — re-renders do not re-fetch (a re-fetch would overwrite
    // a just-saved default with the stale list).
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  it("sets a default with PUT and shows the saved value", async () => {
    mockList.mockResolvedValue([grant()]);
    mockSet.mockResolvedValue(
      grant({ default_context_id: CTX_B, default_context_name: "beta" }),
    );
    render(<ConnectedAppsSection />);

    const select = (await screen.findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() =>
      expect(within(select).getByText("beta")).toBeInTheDocument(),
    );
    fireEvent.change(select, { target: { value: CTX_B } });
    fireEvent.click(screen.getByText("save"));

    await waitFor(() =>
      expect(mockSet).toHaveBeenCalledWith("client-claude", CTX_B),
    );
    expect(mockClear).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("clear")).toBeInTheDocument());
    expect(select.value).toBe(CTX_B);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "saved" }),
    );
  });

  it("clears a default with DELETE", async () => {
    mockList.mockResolvedValue([
      grant({ default_context_id: CTX_A, default_context_name: "alpha" }),
    ]);
    mockClear.mockResolvedValue(undefined);
    render(<ConnectedAppsSection />);

    fireEvent.click(await screen.findByText("clear"));

    await waitFor(() =>
      expect(mockClear).toHaveBeenCalledWith("client-claude"),
    );
    expect(mockSet).not.toHaveBeenCalled();
    const select = screen.getByLabelText(
      "defaultContextFor",
    ) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(""));
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "cleared" }),
    );
  });

  it("choosing 'No default' and saving clears with DELETE", async () => {
    mockList.mockResolvedValue([
      grant({ default_context_id: CTX_A, default_context_name: "alpha" }),
    ]);
    mockClear.mockResolvedValue(undefined);
    render(<ConnectedAppsSection />);

    const select = (await screen.findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(CTX_A));
    fireEvent.change(select, { target: { value: "" } });
    fireEvent.click(screen.getByText("save"));

    await waitFor(() =>
      expect(mockClear).toHaveBeenCalledWith("client-claude"),
    );
  });

  it("keeps a default from another workspace selectable by name", async () => {
    mockList.mockResolvedValue([
      grant({
        default_context_id: "55555555-5555-4555-8555-555555555555",
        default_context_name: "elsewhere",
      }),
    ]);
    render(<ConnectedAppsSection />);

    const select = (await screen.findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() =>
      expect(select.value).toBe("55555555-5555-4555-8555-555555555555"),
    );
    expect(within(select).getByText("elsewhere")).toBeInTheDocument();
  });

  it("reports a refused change and keeps the pending choice", async () => {
    mockList.mockResolvedValue([grant()]);
    mockSet.mockRejectedValue(new Error("Context not found"));
    render(<ConnectedAppsSection />);

    const select = (await screen.findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() =>
      expect(within(select).getByText("Alpha")).toBeInTheDocument(),
    );
    fireEvent.change(select, { target: { value: CTX_A } });
    fireEvent.click(screen.getByText("save"));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          description: "Context not found",
        }),
      ),
    );
    expect(select.value).toBe(CTX_A);
  });
});
