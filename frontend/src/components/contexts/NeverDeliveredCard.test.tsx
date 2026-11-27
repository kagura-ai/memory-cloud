/**
 * NeverDeliveredCard (#3022): the count, the threshold and the exempt tags
 * come from the response; the Review link is a fresh query string; nothing
 * renders while loading, at zero or on an error.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NeverDeliveredCard } from "./NeverDeliveredCard";

const { mockGetCount, mockToast } = vi.hoisted(() => ({
  mockGetCount: vi.fn(),
  mockToast: vi.fn(),
}));

vi.mock("@/lib/api/contexts", () => ({
  getNeverDeliveredCount: (...a: unknown[]) => mockGetCount(...a),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/workspace/contexts/ctx-1",
  // The current URL still carries an earlier Memories visit's filters.
  useSearchParams: () =>
    new URLSearchParams("tab=overview&q=foo&tags=a&memoryId=m-1"),
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

const { stableT } = vi.hoisted(() => ({
  stableT: (k: string, values?: Record<string, unknown>) => {
    if (!values) return k;
    return `${k}:${Object.values(values).map(String).join(",")}`;
  },
}));
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
}));

beforeEach(() => {
  mockGetCount.mockReset();
  mockToast.mockReset();
});

const RESPONSE = { count: 12, older_than_days: 180, exempt_tags: ["status:open", "keep"] };

describe("NeverDeliveredCard", () => {
  it("renders the count, the days and the tags from the response", async () => {
    mockGetCount.mockResolvedValue(RESPONSE);
    render(<NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />);
    expect(await screen.findByText("cardBody:12,180")).toBeInTheDocument();
    expect(screen.getByText("cardTitle")).toBeInTheDocument();
    expect(screen.getByText("cardExcludedWithTags:status:open, keep")).toBeInTheDocument();
    expect(mockGetCount).toHaveBeenCalledWith("ctx-1");
  });

  it("links to a fresh query, never merging the current parameters", async () => {
    mockGetCount.mockResolvedValue(RESPONSE);
    render(<NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />);
    const link = await screen.findByRole("link", { name: "cardReview" });
    expect(link.getAttribute("href")).toBe(
      "/workspace/contexts/ctx-1?tab=memories&never_delivered=1",
    );
    expect(screen.queryByText("cardNoAccess")).toBeNull();
  });

  it("shows a hint instead of the link to a viewer without the Memories tab", async () => {
    mockGetCount.mockResolvedValue(RESPONSE);
    render(<NeverDeliveredCard contextId="ctx-1" canOpenMemories={false} refreshKey={0} />);
    expect(await screen.findByText("cardNoAccess")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("uses the sentence without tags when no tag is exempt", async () => {
    mockGetCount.mockResolvedValue({ ...RESPONSE, exempt_tags: [] });
    render(<NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />);
    expect(await screen.findByText("cardExcluded")).toBeInTheDocument();
  });

  it("renders nothing at zero", async () => {
    mockGetCount.mockResolvedValue({ ...RESPONSE, count: 0 });
    const { container } = render(
      <NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />,
    );
    await waitFor(() => expect(mockGetCount).toHaveBeenCalled());
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while loading", () => {
    mockGetCount.mockReturnValue(new Promise(() => {}));
    const { container } = render(
      <NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing and raises no toast on a failed fetch", async () => {
    mockGetCount.mockRejectedValue(new Error("boom"));
    const { container } = render(
      <NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />,
    );
    await waitFor(() => expect(mockGetCount).toHaveBeenCalled());
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("fetches again when the refresh key changes", async () => {
    mockGetCount.mockResolvedValue(RESPONSE);
    const { rerender } = render(
      <NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={0} />,
    );
    await screen.findByText("cardBody:12,180");
    mockGetCount.mockResolvedValue({ ...RESPONSE, count: 3 });
    rerender(<NeverDeliveredCard contextId="ctx-1" canOpenMemories refreshKey={1} />);
    expect(await screen.findByText("cardBody:3,180")).toBeInTheDocument();
    expect(mockGetCount).toHaveBeenCalledTimes(2);
  });
});
