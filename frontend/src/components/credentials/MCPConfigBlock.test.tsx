/**
 * Tests for MCPConfigBlock.
 *
 * Verifies:
 * - 3 client variants render distinct JSON shapes
 * - localStorage persistence (read on mount, write on tab change)
 * - default-masked state shows "kag_•••" derived from key_prefix, not the live key
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
import {
  MCPConfigBlock,
  CODEX_INSTALL_COMMAND,
  buildTomlConfig,
} from "./MCPConfigBlock";

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
  bound_context_id: null,
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
    it("renders the masked JSON with kag_••• placeholder derived from key_prefix, not the live key", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      const pre = screen.getByText(/Bearer kag_•••••••••••/);
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
        screen.queryByText(/Bearer kag_•••••••••••/),
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
      // Even though the visual mask uses bullets, the clipboard payload
      // must contain the LIVE key, never the mask string.
      expect(written).not.toContain("•••••••••••");
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

    it("clipboard write failures fire a destructive toast with the common error title (not the success title)", async () => {
      mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(screen.getByRole("button", { name: "copyConfig" }));
      await act(async () => {
        await Promise.resolve();
      });

      // Note: the test mocks useTranslations to return the key as-is,
      // so "error" here is the tCommon namespace key (not "mcpConfigCopied").
      expect(mockToast).toHaveBeenCalledWith({
        title: "error",
        description: "clipboard denied",
        variant: "destructive",
      });
    });

    it("non-Error rejections produce a usable description (not empty)", async () => {
      // Some clipboard implementations reject with strings or DOMException
      // — verify we narrow safely instead of hitting an undefined .message.
      mockWriteText.mockRejectedValueOnce("permission denied");
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(screen.getByRole("button", { name: "copyConfig" }));
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockToast).toHaveBeenCalledWith({
        title: "error",
        description: "permission denied",
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

  // Codex CLI tab — independent render path (install command + collapsible
  // manual TOML). Per the existing test pattern (line 130 comment), Radix
  // tab triggers don't respond cleanly to fireEvent.click in happy-dom, so
  // we exercise the codex render path by pre-populating localStorage.
  describe("codex tab", () => {
    beforeEach(() => {
      localStorageStore["kagura_last_mcp_client"] = "codex";
    });

    it("renders the Codex install command verbatim when codex is the active client", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      // The install command is a module-level constant; assert by reading
      // the same export the component renders so a rename catches here.
      expect(screen.getByText(CODEX_INSTALL_COMMAND)).toBeInTheDocument();
      // The JSON shape MUST NOT appear in codex mode (independent path).
      expect(screen.queryByText(/"mcpServers"/)).not.toBeInTheDocument();
    });

    it("install Copy writes CODEX_INSTALL_COMMAND to clipboard and fires the install toast", async () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);

      // Distinct aria-label per Copy button (Copilot PR #817 review):
      // install uses copyInstallCommand, manual TOML uses copyManualConfig,
      // JSON tabs (unmounted in codex mode) use copyConfig.
      fireEvent.click(
        screen.getByRole("button", { name: "copyInstallCommand" }),
      );
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockWriteText).toHaveBeenCalledWith(CODEX_INSTALL_COMMAND);
      // The install toast includes the same "clipboard clears in 60s" hint
      // as the other Copy buttons, because handleInstallCopy routes through
      // useRevealableSecret.copy (which arms the 60s auto-clear).
      expect(mockToast).toHaveBeenCalledWith({
        title: "codexInstallCopied",
        description: "mcpConfigCopiedHint",
      });
    });

    it("install Copy does NOT flash Check on the manual TOML Copy button (cross-button leak regression pin)", async () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      // Expand the manual TOML so its Copy button is in the DOM
      fireEvent.click(
        screen.getByRole("button", { name: "codexManualConfigToggle" }),
      );

      // Press the install Copy
      fireEvent.click(
        screen.getByRole("button", { name: "copyInstallCommand" }),
      );
      await act(async () => {
        await Promise.resolve();
      });

      // The install button SHOULD now contain a Check icon (lucide renders
      // an svg with class "lucide-check"); the manual TOML Copy MUST NOT.
      const installBtn = screen.getByRole("button", {
        name: "copyInstallCommand",
      });
      const tomlBtn = screen.getByRole("button", {
        name: "copyManualConfig",
      });
      expect(installBtn.querySelector("svg.lucide-check")).not.toBeNull();
      expect(tomlBtn.querySelector("svg.lucide-check")).toBeNull();
    });

    it("manual config Collapsible is closed by default — TOML body is hidden", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      // Radix Collapsible renders the content with `data-state="closed"` and
      // hides it from accessibility tree — querying for the TOML body text
      // returns null when collapsed.
      expect(
        screen.queryByText(/\[mcp_servers\.kagura-memory\]/),
      ).not.toBeInTheDocument();
      // The trigger is present and labeled.
      expect(
        screen.getByRole("button", { name: "codexManualConfigToggle" }),
      ).toBeInTheDocument();
    });

    it("expanding manual config reveals the TOML snippet with url and bearer_token rows (masked by default)", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(
        screen.getByRole("button", { name: "codexManualConfigToggle" }),
      );

      // TOML header is rendered
      expect(
        screen.getByText(/\[mcp_servers\.kagura-memory\]/),
      ).toBeInTheDocument();
      // url row carries the MCP_URL
      expect(
        screen.getByText(new RegExp(`url = "${MCP_URL}"`)),
      ).toBeInTheDocument();
      // bearer_token row uses the masked value by default (not the live key)
      expect(
        screen.getByText(/bearer_token = "kag_•••••••••••"/),
      ).toBeInTheDocument();
      expect(screen.queryByText(/kag_real_secret_xyz/)).not.toBeInTheDocument();
    });

    it("manual TOML reveal toggle swaps the masked bearer_token for the live key in display", () => {
      render(<MCPConfigBlock apiKey={VISIBLE_KEY} mcpUrl={MCP_URL} />);
      fireEvent.click(
        screen.getByRole("button", { name: "codexManualConfigToggle" }),
      );
      fireEvent.click(screen.getByRole("button", { name: "showKey" }));

      expect(
        screen.getByText(/bearer_token = "kag_real_secret_xyz"/),
      ).toBeInTheDocument();
      expect(
        screen.queryByText(/bearer_token = "kag_•••••••••••"/),
      ).not.toBeInTheDocument();
    });

    it("manual TOML Copy is disabled and placeholder is shown when apiKey is null", () => {
      render(<MCPConfigBlock apiKey={null} mcpUrl={MCP_URL} />);
      fireEvent.click(
        screen.getByRole("button", { name: "codexManualConfigToggle" }),
      );

      // Placeholder value is rendered in the bearer_token row
      expect(
        screen.getByText(/bearer_token = "YOUR_API_KEY"/),
      ).toBeInTheDocument();
      // The manual TOML Copy is the second `mcpConfigHiddenCopyDisabled`-
      // labeled button (the first lives in the JSON tab's render path,
      // which is not rendered when codex is active — so we actually expect
      // exactly one disabled Copy here).
      const disabledCopies = screen.getAllByRole("button", {
        name: "mcpConfigHiddenCopyDisabled",
      });
      expect(disabledCopies).toHaveLength(1);
      expect(disabledCopies[0]).toBeDisabled();
    });
  });

  // buildTomlConfig — direct helper tests (the TOML escape contract is
  // hand-rolled, unlike JSON.stringify, so a focused unit test on the
  // helper itself is cheaper than threading escape-needing keys through
  // the full render path.
  describe("buildTomlConfig", () => {
    it("escapes backslashes and quotes in the authValue", () => {
      const out = buildTomlConfig(
        "https://example.com/mcp",
        'kag_a\\b"c', // raw: kag_a\b"c
      );
      // After escape, the literal in the TOML basic string is `kag_a\\b\"c`.
      // We assert by checking the exact bearer_token row to pin both the
      // backslash AND the quote escape in one shot.
      expect(out).toContain('bearer_token = "kag_a\\\\b\\"c"');
    });

    it("escapes backslashes and quotes in the mcpUrl too", () => {
      const out = buildTomlConfig('https://ex\\amp"le.com/mcp', "tok");
      expect(out).toContain('url = "https://ex\\\\amp\\"le.com/mcp"');
    });

    it("escapes raw control characters with \\uXXXX (TOML basic strings forbid raw controls)", () => {
      // Common whitespace controls (\n, \r, \t) get named escapes; other
      // C0 controls + DEL get numeric \uXXXX escapes. Together the helper
      // produces parse-safe TOML even if a refactor leaks control chars.
      const out = buildTomlConfig(
        "https://example.com/mcp",
        "tok\nA\rB\tC\bDE",
      );
      expect(out).toContain('bearer_token = "tok\\nA\\rB\\tC\\u0008D\\u007fE"');
    });

    it("warns in dev mode if the inputs contain control characters", () => {
      // process.env.NODE_ENV is typed as string under the project's
      // tsconfig, so direct assignment type-checks without a suppression
      // (Copilot PR #817 review caught the unused @ts-expect-error).
      const originalEnv = process.env.NODE_ENV;
      process.env.NODE_ENV = "development";
      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

      try {
        buildTomlConfig("https://example.com/mcp\ninjected", "tok");
        expect(warnSpy).toHaveBeenCalledTimes(1);
        expect(warnSpy.mock.calls[0][0]).toContain(
          "[MCPConfigBlock.buildTomlConfig]",
        );
      } finally {
        warnSpy.mockRestore();
        process.env.NODE_ENV = originalEnv;
      }
    });
  });
});
