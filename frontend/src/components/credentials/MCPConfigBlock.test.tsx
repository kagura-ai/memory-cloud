/**
 * Tests for MCPConfigBlock.
 *
 * Verifies:
 * - 3 client variants render distinct JSON shapes
 * - localStorage persistence (read on mount, write on tab change)
 * - default-masked state shows "sk-•••••••••••" not the live key
 * - Show toggle reveals the live key in the displayed JSON
 * - Copy writes the JSON containing the LIVE key (not the mask)
 * - hidden state (apiKey null) shows "YOUR_API_KEY" placeholder and
 *   disables Copy
 * - visible→hidden transition force-hides revealed state and removes
 *   the live key from the DOM (regression test from CSO pre-review)
 * - 60s clipboard auto-clear (fake timers)
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import type { MemberAPIKey } from "@/lib/api/member-credentials";
import { MCPConfigBlock } from "./MCPConfigBlock";

// Stable references defined OUTSIDE beforeEach so React's useCallback /
// useEffect dependency arrays see constant references — matches the
// canonical pattern from sleep-reports/page.test.tsx.
const mockToast = vi.fn();
const stableToastCtx = { toast: mockToast };
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => stableToastCtx,
}));

const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: () => stableTranslator,
}));

const mockWriteText = vi.fn().mockResolvedValue(undefined);

const localStorageStore: Record<string, string> = {};
const localStorageMock = {
  getItem: vi.fn((k: string) => localStorageStore[k] ?? null),
  setItem: vi.fn((k: string, v: string) => {
    localStorageStore[k] = v;
  }),
  removeItem: vi.fn((k: string) => {
    delete localStorageStore[k];
  }),
  clear: vi.fn(() => {
    for (const k of Object.keys(localStorageStore)) delete localStorageStore[k];
  }),
};

beforeEach(() => {
  vi.useFakeTimers();
  mockToast.mockReset();
  mockWriteText.mockReset();
  mockWriteText.mockResolvedValue(undefined);
  for (const k of Object.keys(localStorageStore)) delete localStorageStore[k];
  localStorageMock.getItem.mockClear();
  localStorageMock.setItem.mockClear();

  Object.defineProperty(window, "localStorage", {
    value: localStorageMock,
    writable: true,
    configurable: true,
  });
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: mockWriteText },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

const VISIBLE_KEY: MemberAPIKey = {
  id: 1,
  name: "test-key",
  key_prefix: "kag_",
  plaintext_key: "kag_real_secret_xyz",
  is_visible: true,
  visibility_expires_at: "2099-01-01T00:00:00Z",
  created_at: "2026-04-01T00:00:00Z",
  revoked_at: null,
};

const HIDDEN_KEY: MemberAPIKey = {
  ...VISIBLE_KEY,
  plaintext_key: null,
  is_visible: false,
  visibility_expires_at: null,
};

const MCP_URL = "https://memory.kagura-ai.com/mcp/w/test-ws";

describe("MCPConfigBlock", () => {
  describe("default rendering (visible window, masked)", () => {
    it("renders the masked JSON with sk-••• placeholder, not the live key", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      const pre = screen.getByText(/Bearer sk-•••••••••••/);
      expect(pre).toBeInTheDocument();
      expect(screen.queryByText(/kag_real_secret_xyz/)).not.toBeInTheDocument();
    });

    it("Show and Copy buttons are enabled when key is visible", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      expect(screen.getByRole("button", { name: "showKey" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "copyConfig" })).toBeEnabled();
    });

    it("defaults to claude-code tab when localStorage is empty", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      const tab = screen.getByRole("tab", { name: "clients.claudeCode" });
      expect(tab).toHaveAttribute("aria-selected", "true");
    });

    it("renders the mcpServers JSON shape for claude-code", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      // The full JSON is in a <pre>; check for distinctive substrings
      expect(screen.getByText(/"mcpServers"/)).toBeInTheDocument();
      expect(screen.getByText(/"kagura-memory"/)).toBeInTheDocument();
      expect(
        screen.getByText(new RegExp(`"url": "${MCP_URL}"`)),
      ).toBeInTheDocument();
    });
  });

  describe("client tabs", () => {
    // Note on test approach: Radix Tabs Triggers do not respond cleanly to
    // fireEvent.click in happy-dom (they rely on pointer events). Rather
    // than introduce @testing-library/user-event as a new dependency, we
    // exercise the persistence behavior by pre-populating localStorage
    // and verifying the mount-time read path. This covers the user-visible
    // contract (the tab is restored on next visit) without depending on
    // Radix's internal pointer handling.

    it("renders the chatgpt connector instructions when chatgpt is the active client", () => {
      localStorageStore["kagura_last_mcp_client"] = "chatgpt";
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);

      expect(
        screen.getByText(/ChatGPT → Settings → Custom Connectors/),
      ).toBeInTheDocument();
      // The mcpServers shape should NOT be visible in chatgpt mode
      expect(screen.queryByText(/"mcpServers"/)).not.toBeInTheDocument();
    });

    it("persists the active client to localStorage on mount and on changes", () => {
      // Mount with default → setItem fires with "claude-code"
      const { unmount } = render(
        <MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />,
      );
      expect(localStorageMock.setItem).toHaveBeenCalledWith(
        "kagura_last_mcp_client",
        "claude-code",
      );
      unmount();

      // Mount with cursor pre-populated → restore + setItem fires again
      localStorageMock.setItem.mockClear();
      localStorageStore["kagura_last_mcp_client"] = "cursor";
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);

      expect(localStorageMock.setItem).toHaveBeenCalledWith(
        "kagura_last_mcp_client",
        "cursor",
      );
    });

    it("restores the persisted tab on mount", () => {
      localStorageStore["kagura_last_mcp_client"] = "chatgpt";
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);

      const chatgptTab = screen.getByRole("tab", { name: "clients.chatgpt" });
      expect(chatgptTab).toHaveAttribute("aria-selected", "true");
    });
  });

  describe("Show toggle", () => {
    it("revealing displays the live key in the JSON", async () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);

      fireEvent.click(screen.getByRole("button", { name: "showKey" }));

      expect(
        screen.getByText(/Bearer kag_real_secret_xyz/),
      ).toBeInTheDocument();
      expect(
        screen.queryByText(/Bearer sk-•••••••••••/),
      ).not.toBeInTheDocument();
    });
  });

  describe("Copy", () => {
    it("writes the JSON with the LIVE key to clipboard regardless of mask state", async () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);

      // Do NOT click Show — the visible JSON is masked, but Copy should
      // still write the live value
      fireEvent.click(screen.getByRole("button", { name: "copyConfig" }));
      // copy() is async; flush microtasks so the writeText call settles.
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockWriteText).toHaveBeenCalledTimes(1);
      const written = mockWriteText.mock.calls[0][0] as string;
      expect(written).toContain("kag_real_secret_xyz");
      expect(written).not.toContain("sk-•••••••••••");
    });

    it("fires the consumer toast on success", async () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(screen.getByRole("button", { name: "copyConfig" }));
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockToast).toHaveBeenCalledWith({
        title: "mcpConfigCopied",
        description: "mcpConfigCopiedHint",
      });
    });

    it("clipboard write failures fire a destructive toast", async () => {
      mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(screen.getByRole("button", { name: "copyConfig" }));
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockToast).toHaveBeenCalledWith({
        title: "mcpConfigCopied",
        description: "clipboard denied",
        variant: "destructive",
      });
    });

    it("clears clipboard 60s after a successful copy", async () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(screen.getByRole("button", { name: "copyConfig" }));
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockWriteText).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(60_001);
        await Promise.resolve();
      });

      expect(mockWriteText).toHaveBeenCalledTimes(2);
      expect(mockWriteText).toHaveBeenLastCalledWith("");
    });
  });

  describe("hidden state (visibility window expired)", () => {
    it("renders YOUR_API_KEY placeholder when apiKey is null", () => {
      render(<MCPConfigBlock apiKey={null} mcpUrl={MCP_URL} />);
      expect(screen.getByText(/Bearer YOUR_API_KEY/)).toBeInTheDocument();
    });

    it("disables both Show and Copy when apiKey is null", () => {
      render(<MCPConfigBlock apiKey={null} mcpUrl={MCP_URL} />);
      expect(screen.getByRole("button", { name: "showKey" })).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "mcpConfigHiddenCopyDisabled" }),
      ).toBeDisabled();
    });

    it("disables Copy when apiKey.is_visible is false (window expired)", () => {
      render(<MCPConfigBlock apiKey={HIDDEN_KEY} mcpUrl={MCP_URL} />);
      expect(
        screen.getByRole("button", { name: "mcpConfigHiddenCopyDisabled" }),
      ).toBeDisabled();
    });
  });

  describe("visible→hidden transition (regression)", () => {
    it("force-hides revealed state and removes live key from DOM when key transitions to hidden", () => {
      const { rerender } = render(
        <MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />,
      );

      // Reveal first
      fireEvent.click(screen.getByRole("button", { name: "showKey" }));
      expect(
        screen.getByText(/Bearer kag_real_secret_xyz/),
      ).toBeInTheDocument();

      // Simulate visibility window expiring (apiKey becomes null OR hidden)
      rerender(<MCPConfigBlock apiKey={HIDDEN_KEY} mcpUrl={MCP_URL} />);

      // Live key MUST NOT remain in the DOM
      expect(screen.queryByText(/kag_real_secret_xyz/)).not.toBeInTheDocument();
      // Placeholder is back
      expect(screen.getByText(/Bearer YOUR_API_KEY/)).toBeInTheDocument();
      // Toggle is now disabled
      expect(screen.getByRole("button", { name: "showKey" })).toBeDisabled();
    });
  });
});
