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

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { McpConnectionCard, McpReferenceLinks } from "./McpConnectionCard";

vi.mock("next-intl", () => ({
  useTranslations: () => Object.assign((key: string) => key, { has: () => false }),
}));

const mockWorkspace = {
  currentWorkspaceId: "ws-1" as string | null,
  workspaces: [{ id: "ws-1" }, { id: "ws-2" }] as { id: string }[],
};
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockWorkspace,
}));

vi.mock("@/lib/api/base", () => ({
  apiClient: { get: vi.fn().mockResolvedValue({ tools: [
    { name: "explore", description: "Explore related memories" },
    { name: "setup_resource", description: "Create a resource" },
  ] }) },
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

// #2760: the ChatGPT app link reads the deployment's listing from /system/info.
let mockSystemInfo: { chatgpt_app_url?: string | null } | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemInfo: () => mockSystemInfo }));

beforeEach(() => {
  vi.clearAllMocks();
  mockCopyToTarget.mockResolvedValue(undefined);
  mockWorkspace.currentWorkspaceId = "ws-1";
  mockWorkspace.workspaces = [{ id: "ws-1" }, { id: "ws-2" }];
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://memory.example.test/api/v1");
  mockSystemInfo = null;
});

describe("McpConnectionCard — ChatGPT app link (#2760)", () => {
  const LISTING = "https://example.com/chatgpt-app";
  // useId values move with every render; the markup around them must not.
  const markup = (el: HTMLElement) => el.innerHTML.replace(/_r_[0-9a-z]+_/g, "_id_");
  const chatGptLink = () => screen.queryAllByRole("link").filter((a) => a.getAttribute("href") === LISTING);

  it.each(["summary", "full"] as const)("adds no link and no extra DOM to the %s card when the deployment sets none", (variant) => {
    const unset = render(<McpConnectionCard variant={variant} />);
    const html = markup(unset.container);
    unset.unmount();
    for (const info of [{ chatgpt_app_url: null }, { chatgpt_app_url: "http://example.com/chatgpt-app" }, { chatgpt_app_url: "javascript:alert(1)" }]) {
      mockSystemInfo = info;
      const { container, unmount } = render(<McpConnectionCard variant={variant} />);
      expect(chatGptLink()).toHaveLength(0);
      expect(markup(container)).toBe(html);
      unmount();
    }
  });

  it.each(["summary", "full"] as const)("links the listing in a new tab on the %s card, next to the plugin guide", (variant) => {
    mockSystemInfo = { chatgpt_app_url: LISTING };
    render(<McpConnectionCard variant={variant} />);
    const [link] = chatGptLink();
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAccessibleName("label newTab");
    const pluginGuide = screen.getAllByRole("link").find((a) => a.getAttribute("href")?.includes("/help/guide/#plugins"));
    expect(link.parentElement?.nextElementSibling).toContainElement(pluginGuide ?? null);
  });
});

describe("McpConnectionCard — overview tool settings", () => {
  it("keeps the displayed and copied URL in sync for full and added tools", async () => {
    render(<McpConnectionCard variant="summary" />);
    fireEvent.click(screen.getByRole("button", { name: "quickStart.toolSettings" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /explore/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /setup_resource/ }));
    const extraUrl = "https://memory.example.test/mcp?add_tools=explore,setup_resource";
    expect(screen.getByText(extraUrl)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "copyUrl" }));
    expect(mockCopyToTarget).toHaveBeenLastCalledWith(extraUrl, "mcp-url");
    fireEvent.click(screen.getByRole("switch", { name: "quickStart.allTools" }));
    expect(screen.getByRole("checkbox", { name: /explore/ })).toBeDisabled();
    expect(screen.getByText("https://memory.example.test/mcp?profile=full")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "copyUrl" }));
    expect(mockCopyToTarget).toHaveBeenLastCalledWith("https://memory.example.test/mcp?profile=full", "mcp-url");
  });
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("McpConnectionCard — endpoint", () => {
  it("shows the bare URL and the workspace-pinned URL", () => {
    render(<McpConnectionCard />);

    expect(
      screen.getByText("https://memory.example.test/mcp"),
    ).toBeInTheDocument();
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

  it("'All tools' puts ?profile=full on the URL, the command and the pinned URL (#1609, #1849)", () => {
    render(<McpConnectionCard />);

    fireEvent.click(screen.getByRole("switch"));

    expect(
      screen.getByText("https://memory.example.test/mcp?profile=full"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'claude mcp add --transport http kagura-memory "https://memory.example.test/mcp?profile=full"',
      ),
    ).toBeInTheDocument();

    expect(
      screen.getByText("https://memory.example.test/mcp/w/ws-1?profile=full"),
    ).toBeInTheDocument();
  });
});

describe("McpConnectionCard — workspace pinning", () => {
  it("shows the pinned URL directly for the selected workspace", () => {
    render(<McpConnectionCard />);

    expect(screen.getByText("pinHint")).toBeInTheDocument();


    expect(
      screen.getByText("https://memory.example.test/mcp/w/ws-1"),
    ).toBeInTheDocument();
  });

  it("shows the pinned URL for a single-workspace user", () => {
    mockWorkspace.workspaces = [{ id: "ws-1" }];

    render(<McpConnectionCard />);

    expect(screen.getByText("pinToggle")).toBeInTheDocument();
    expect(screen.getByText("https://memory.example.test/mcp/w/ws-1")).toBeInTheDocument();
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

describe("McpConnectionCard — overview summary", () => {
  it.each(["summary", "full"] as const)("links the optional plugin guide from %s", (variant) => {
    render(<McpConnectionCard variant={variant} />);
    expect(screen.getByRole("link", { name: "label newTab" })).toHaveAttribute("href", "https://www.kagura-ai.com/help/guide/#plugins");
  });
  it("presents the bare URL with a labelled copy button and a guide link", () => {
    render(<McpConnectionCard variant="summary" />);
    expect(screen.getByRole("heading", { name: "quickStart.title" })).toBeInTheDocument();
    expect(screen.getByText("https://memory.example.test/mcp")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "copyUrl" }));
    expect(mockCopyToTarget).toHaveBeenCalledWith("https://memory.example.test/mcp", "mcp-url");
    expect(screen.getByRole("link", { name: "links.mcpClients" })).toHaveAttribute(
      "href", "https://github.com/kagura-ai/memory-cloud/blob/main/docs/mcp-clients.md",
    );
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByText(/claude mcp add/)).toBeNull();
    expect(screen.getByText("followsWorkspace")).toBeInTheDocument();
  });

  it("offers manual copy guidance when clipboard access fails", async () => {
    mockCopyToTarget.mockRejectedValueOnce(new Error("clipboard unavailable"));
    render(<McpConnectionCard variant="summary" />);
    fireEvent.click(screen.getByRole("button", { name: "copyUrl" }));
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith({
      title: "error",
      description: "copyFailedManualHint",
      variant: "destructive",
    }));
  });
});

describe("MCP connection references", () => {
  it("includes client setup, both SDKs and REST API documentation", () => {
    render(<McpReferenceLinks />);
    expect(screen.getByRole("link", { name: "links.mcpClients" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "links.typescriptSdk" })).toHaveAttribute("href", "https://github.com/kagura-ai/kagura-memory-typescript-sdk");
    expect(screen.getByRole("link", { name: "links.pythonSdk" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "links.restApi" })).toHaveAttribute("href", "https://memory.example.test/redoc");
  });
});
