/**
 * Tests for McpConnectionCard (#1836).
 *
 * The card is the one place on the credentials page that shows the MCP
 * endpoint. It leads with the bare `…/mcp` URL (OAuth resolves the workspace
 * at login; an API key carries its own workspace), and only offers the
 * workspace-pinned `/mcp/w/<id>` form when the user belongs to more than one
 * workspace — the one case where an OAuth connector would otherwise follow
 * the workspace selected in the Web UI.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { McpConnectionCard } from "./McpConnectionCard";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

const mockWorkspace = {
  currentWorkspaceId: "ws-1" as string | null,
  workspaces: [{ id: "ws-1" }, { id: "ws-2" }] as { id: string }[],
};
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockWorkspace,
}));

const mockCopyToTarget = vi.fn();
vi.mock("@/hooks/useCopyFeedback", () => ({
  useCopyFeedback: () => ({
    isCopied: () => false,
    copyToTarget: (...a: unknown[]) => mockCopyToTarget(...a),
  }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockCopyToTarget.mockResolvedValue(undefined);
  mockWorkspace.currentWorkspaceId = "ws-1";
  mockWorkspace.workspaces = [{ id: "ws-1" }, { id: "ws-2" }];
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://memory.example.test/api/v1");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("McpConnectionCard — endpoint", () => {
  it("leads with the bare /mcp URL and never shows /mcp/w/ by default", () => {
    render(<McpConnectionCard />);

    expect(
      screen.getByText("https://memory.example.test/mcp"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\/mcp\/w\//)).not.toBeInTheDocument();
  });

  it("copies the bare URL from the copy button", () => {
    render(<McpConnectionCard />);

    fireEvent.click(screen.getByLabelText("copyUrl"));

    expect(mockCopyToTarget).toHaveBeenCalledWith(
      "https://memory.example.test/mcp",
      "mcp-url",
    );
  });

  it("renders the Claude Code OAuth one-liner against the bare URL", () => {
    render(<McpConnectionCard />);

    expect(
      screen.getByText(
        "claude mcp add --transport http kagura-memory https://memory.example.test/mcp",
      ),
    ).toBeInTheDocument();
  });

  it("tolerates a trailing slash on NEXT_PUBLIC_API_URL", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://memory.example.test/api/v1/");

    render(<McpConnectionCard />);

    expect(
      screen.getByText("https://memory.example.test/mcp"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\/\/mcp/)).not.toBeInTheDocument();
  });

  it("'Core tools only' puts ?profile=core on the URL, the command and the pinned URL (#1609)", () => {
    render(<McpConnectionCard />);

    fireEvent.click(screen.getByRole("switch"));

    expect(
      screen.getByText("https://memory.example.test/mcp?profile=core"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'claude mcp add --transport http kagura-memory "https://memory.example.test/mcp?profile=core"',
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText("pinToggle"));
    expect(
      screen.getByText("https://memory.example.test/mcp/w/ws-1?profile=core"),
    ).toBeInTheDocument();
  });
});

describe("McpConnectionCard — workspace pinning", () => {
  it("offers the pinned URL only after expanding, when the user has several workspaces", () => {
    render(<McpConnectionCard />);

    expect(screen.getByText("followsWorkspace")).toBeInTheDocument();
    expect(screen.queryByText(/\/mcp\/w\/ws-1/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("pinToggle"));

    expect(
      screen.getByText("https://memory.example.test/mcp/w/ws-1"),
    ).toBeInTheDocument();
  });

  it("hides the pinning section for a single-workspace user", () => {
    mockWorkspace.workspaces = [{ id: "ws-1" }];

    render(<McpConnectionCard />);

    expect(screen.queryByText("followsWorkspace")).not.toBeInTheDocument();
    expect(screen.queryByText("pinToggle")).not.toBeInTheDocument();
  });

  it("hides the pinning section when no workspace is selected", () => {
    mockWorkspace.currentWorkspaceId = null;

    render(<McpConnectionCard />);

    expect(screen.queryByText("pinToggle")).not.toBeInTheDocument();
    expect(
      screen.getByText("https://memory.example.test/mcp"),
    ).toBeInTheDocument();
  });
});
