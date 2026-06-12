/**
 * Tests for the copyText clipboard utility (issue #987).
 *
 * Contract:
 * - Prefer the async Clipboard API (navigator.clipboard.writeText).
 * - Fall back to the legacy document.execCommand('copy') via a hidden
 *   textarea when the async API is missing OR rejects (e.g. NotAllowedError
 *   "Write permission denied" when the document is not focused / insecure
 *   origin / embedded webview).
 * - Throw a typed ClipboardCopyError only when BOTH paths fail, so callers
 *   can render an actionable, i18n'd message instead of a raw DOM exception.
 * - The fallback textarea (which may transiently hold a plaintext secret) is
 *   removed synchronously and prior focus is restored.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { copyText, ClipboardCopyError } from "./clipboard";

const mockWriteText = vi.fn();

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", {
    value,
    writable: true,
    configurable: true,
  });
}

beforeEach(() => {
  mockWriteText.mockReset();
  mockWriteText.mockResolvedValue(undefined);
  setClipboard({ writeText: mockWriteText });
  // jsdom does not implement execCommand; provide a controllable stub.
  Object.defineProperty(document, "execCommand", {
    value: vi.fn(() => true),
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("copyText", () => {
  it("writes via the async Clipboard API when available", async () => {
    await copyText("hello");
    expect(mockWriteText).toHaveBeenCalledWith("hello");
    expect(document.execCommand).not.toHaveBeenCalled();
  });

  it("falls back to execCommand('copy') when writeText rejects (NotAllowedError)", async () => {
    mockWriteText.mockRejectedValueOnce(
      new DOMException("Write permission denied", "NotAllowedError"),
    );
    await copyText("api-key-value");
    expect(mockWriteText).toHaveBeenCalledWith("api-key-value");
    expect(document.execCommand).toHaveBeenCalledWith("copy");
  });

  it("falls back to execCommand when the async Clipboard API is absent", async () => {
    setClipboard(undefined);
    await copyText("value");
    expect(document.execCommand).toHaveBeenCalledWith("copy");
  });

  it("throws ClipboardCopyError when both paths fail", async () => {
    mockWriteText.mockRejectedValueOnce(new Error("denied"));
    (document.execCommand as ReturnType<typeof vi.fn>).mockReturnValue(false);
    await expect(copyText("x")).rejects.toBeInstanceOf(ClipboardCopyError);
  });

  it("throws ClipboardCopyError when no copy mechanism exists at all", async () => {
    setClipboard(undefined);
    (document.execCommand as ReturnType<typeof vi.fn>).mockReturnValue(false);
    await expect(copyText("x")).rejects.toBeInstanceOf(ClipboardCopyError);
  });

  it("removes the fallback textarea synchronously (no plaintext lingers in the DOM)", async () => {
    setClipboard(undefined);
    await copyText("super-secret");
    expect(document.querySelector("textarea")).toBeNull();
    // The secret must not be findable anywhere in the DOM after the copy.
    expect(document.body.innerHTML).not.toContain("super-secret");
  });

  it("removes the fallback textarea even when execCommand throws", async () => {
    setClipboard(undefined);
    (document.execCommand as ReturnType<typeof vi.fn>).mockImplementation(() => {
      throw new Error("execCommand blew up");
    });
    await expect(copyText("secret")).rejects.toBeInstanceOf(ClipboardCopyError);
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("restores focus to the previously focused element after the fallback", async () => {
    setClipboard(undefined);
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    expect(document.activeElement).toBe(input);

    await copyText("value");

    expect(document.activeElement).toBe(input);
    input.remove();
  });
});
