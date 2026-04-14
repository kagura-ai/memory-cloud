/**
 * Tests for ResourceDataTabPlaceholder.
 *
 * Verifies:
 * - EmptyState renders with placeholder copy
 * - #316 follow-up link is present with correct URL
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResourceDataTabPlaceholder } from "./ResourceDataTabPlaceholder";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

describe("ResourceDataTabPlaceholder", () => {
  it("renders placeholder title and description", () => {
    render(<ResourceDataTabPlaceholder />);
    expect(screen.getByText("comingSoonTitle")).toBeInTheDocument();
    expect(screen.getByText("comingSoonDescription")).toBeInTheDocument();
  });

  it("links to follow-up issue #316", () => {
    render(<ResourceDataTabPlaceholder />);
    const link = screen.getByRole("link", { name: /trackProgress/ });
    expect(link).toHaveAttribute(
      "href",
      "https://github.com/kagura-ai/memory-cloud/issues/316",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
});
