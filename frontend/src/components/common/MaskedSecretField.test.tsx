/**
 * Tests for MaskedSecretField primitive.
 *
 * Verifies:
 * - default-masked rendering (Show button visible, value hidden)
 * - explicit Show toggle reveals the value
 * - Copy button writes the live value (not the mask) to clipboard
 * - value=null disables Show and Copy
 * - visible→hidden transition force-hides any revealed state and removes
 *   the live value from the DOM (regression test from CSO pre-review)
 * - toast is fired with the consumer-supplied title/description
 * - consumer-supplied i18n labels appear as aria-label / title on buttons
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { MaskedSecretField } from "./MaskedSecretField";

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockWriteText = vi.fn().mockResolvedValue(undefined);

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
});

afterEach(() => {
  vi.useRealTimers();
});

const baseProps = {
  prefix: "Bearer ",
  displayMask: "sk-•••••••••••",
  copyToastTitle: "Config copied",
  copyToastDescription: "Clipboard clears in 60 seconds — paste it now.",
  showLabel: "Show key",
  hideLabel: "Hide key",
  copyLabel: "Copy to clipboard",
};

describe("MaskedSecretField", () => {
  describe("default rendering", () => {
    it("renders the prefix + mask when value is set", () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      expect(screen.getByText("Bearer sk-•••••••••••")).toBeInTheDocument();
    });

    it("does NOT render the live value initially", () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      expect(screen.queryByText(/kag_real_secret/)).not.toBeInTheDocument();
    });

    it("shows the Eye (show) icon by default with showLabel as aria-label", () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      const showBtn = screen.getByRole("button", { name: "Show key" });
      expect(showBtn).toBeEnabled();
      expect(showBtn).toHaveAttribute("aria-pressed", "false");
    });

    it("shows the Copy button with copyLabel as aria-label", () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      const copyBtn = screen.getByRole("button", { name: "Copy to clipboard" });
      expect(copyBtn).toBeEnabled();
    });
  });

  describe("Show toggle", () => {
    it("clicking Show reveals the live value", async () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(screen.getByRole("button", { name: "Show key" }));

      expect(screen.getByText("Bearer kag_real_secret")).toBeInTheDocument();
      expect(
        screen.queryByText("Bearer sk-•••••••••••"),
      ).not.toBeInTheDocument();
    });

    it("after Show, the toggle button becomes Hide and aria-pressed=true", async () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(screen.getByRole("button", { name: "Show key" }));

      const hideBtn = screen.getByRole("button", { name: "Hide key" });
      expect(hideBtn).toHaveAttribute("aria-pressed", "true");
    });

    it("clicking Hide returns to masked state", () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(screen.getByRole("button", { name: "Show key" }));
      fireEvent.click(screen.getByRole("button", { name: "Hide key" }));

      expect(screen.getByText("Bearer sk-•••••••••••")).toBeInTheDocument();
      expect(screen.queryByText(/kag_real_secret/)).not.toBeInTheDocument();
    });
  });

  describe("Copy action", () => {
    it("writes the live value to clipboard (not the mask)", async () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(
        screen.getByRole("button", { name: "Copy to clipboard" }),
      );

      expect(mockWriteText).toHaveBeenCalledWith("kag_real_secret");
    });

    it("fires the consumer-supplied success toast", async () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(
        screen.getByRole("button", { name: "Copy to clipboard" }),
      );
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockToast).toHaveBeenCalledWith({
        title: "Config copied",
        description: "Clipboard clears in 60 seconds — paste it now.",
      });
    });

    it("Copy works regardless of revealed state (mask still on screen)", () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      // Do NOT click Show — copy should still write the live value
      fireEvent.click(
        screen.getByRole("button", { name: "Copy to clipboard" }),
      );

      expect(mockWriteText).toHaveBeenCalledWith("kag_real_secret");
      // Visual stays masked
      expect(screen.getByText("Bearer sk-•••••••••••")).toBeInTheDocument();
    });

    it("clipboard write failures fire a destructive toast", async () => {
      mockWriteText.mockRejectedValueOnce(new Error("clipboard denied"));
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(
        screen.getByRole("button", { name: "Copy to clipboard" }),
      );
      await act(async () => {
        await Promise.resolve();
      });

      expect(mockToast).toHaveBeenCalledWith({
        title: "Config copied",
        description: "clipboard denied",
        variant: "destructive",
      });
    });
  });

  describe("disabled (value === null)", () => {
    it("disables both Show and Copy buttons when value is null", () => {
      render(<MaskedSecretField {...baseProps} value={null} />);
      expect(screen.getByRole("button", { name: "Show key" })).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Copy to clipboard" }),
      ).toBeDisabled();
    });

    it("renders the mask string in the placeholder slot", () => {
      render(<MaskedSecretField {...baseProps} value={null} />);
      expect(screen.getByText("Bearer sk-•••••••••••")).toBeInTheDocument();
    });
  });

  describe("visible→hidden transition (regression)", () => {
    it("force-hides any revealed state when value transitions to null", () => {
      const { rerender } = render(
        <MaskedSecretField {...baseProps} value="kag_real_secret" />,
      );
      // Reveal first
      fireEvent.click(screen.getByRole("button", { name: "Show key" }));
      expect(screen.getByText("Bearer kag_real_secret")).toBeInTheDocument();

      // Simulate the visibility window expiring (value becomes null)
      rerender(<MaskedSecretField {...baseProps} value={null} />);

      // Live value MUST NOT be in the DOM after the transition
      expect(screen.queryByText(/kag_real_secret/)).not.toBeInTheDocument();
      // Mask is back
      expect(screen.getByText("Bearer sk-•••••••••••")).toBeInTheDocument();
      // Toggle is now disabled
      expect(screen.getByRole("button", { name: "Show key" })).toBeDisabled();
    });
  });

  describe("clipboard auto-clear", () => {
    it("clears clipboard 60s after a copy by default", async () => {
      render(<MaskedSecretField {...baseProps} value="kag_real_secret" />);
      fireEvent.click(
        screen.getByRole("button", { name: "Copy to clipboard" }),
      );
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
});
