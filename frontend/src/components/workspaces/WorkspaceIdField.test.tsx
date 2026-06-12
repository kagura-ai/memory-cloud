/**
 * Tests for WorkspaceIdField (Issue #873).
 *
 * Covers the two acceptance criteria the component owns:
 * - the workspace ID renders read-only on screen
 * - the copy button writes the ID to the clipboard and surfaces feedback
 *   (success toast on success, destructive toast on failure)
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { WorkspaceIdField } from "./WorkspaceIdField";

// Stable references defined OUTSIDE beforeEach so hook dependency arrays see
// constant references — matches the canonical MCPConfigBlock.test.tsx pattern.
const mockToast = vi.fn();
const stableToastCtx = { toast: mockToast };
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => stableToastCtx,
}));

// stableTranslator maps every key to itself, so assertions reference the raw
// i18n key (e.g. "copyWorkspaceId") rather than localized prose.
const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: () => stableTranslator,
}));

const mockWriteText = vi.fn().mockResolvedValue(undefined);

const WORKSPACE_ID = "11111111-2222-3333-4444-555555555555";

beforeEach(() => {
  vi.useFakeTimers();
  mockToast.mockReset();
  mockWriteText.mockReset();
  mockWriteText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: mockWriteText },
    writable: true,
    configurable: true,
  });
  // copyText (issue #987) falls back to execCommand when writeText rejects.
  // Default it to "unavailable" so the failure-path test is deterministic.
  Object.defineProperty(document, "execCommand", {
    value: vi.fn(() => false),
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WorkspaceIdField", () => {
  it("renders the workspace ID as read-only text", () => {
    render(<WorkspaceIdField workspaceId={WORKSPACE_ID} />);
    expect(screen.getByText(WORKSPACE_ID)).toBeInTheDocument();
    // There is no editable input for the ID — it is display-only.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("copies the ID to the clipboard and fires a success toast", async () => {
    render(<WorkspaceIdField workspaceId={WORKSPACE_ID} />);

    fireEvent.click(screen.getByRole("button", { name: "copyWorkspaceId" }));
    // copy() is async; flush microtasks so the writeText call settles.
    await act(async () => {});

    expect(mockWriteText).toHaveBeenCalledTimes(1);
    expect(mockWriteText).toHaveBeenCalledWith(WORKSPACE_ID);
    expect(mockToast).toHaveBeenCalledWith({
      title: "success",
      description: "workspaceIdCopied",
    });
  });

  it("fires a destructive toast with an actionable hint when both clipboard and fallback fail", async () => {
    mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));
    render(<WorkspaceIdField workspaceId={WORKSPACE_ID} />);

    fireEvent.click(screen.getByRole("button", { name: "copyWorkspaceId" }));
    await act(async () => {});

    // copyText threw (writeText denied + execCommand unavailable) — the toast
    // shows the actionable i18n hint, NOT the raw DOM exception string.
    expect(mockToast).toHaveBeenCalledWith({
      variant: "destructive",
      title: "error",
      description: "copyFailedManualHint",
    });
  });
});
