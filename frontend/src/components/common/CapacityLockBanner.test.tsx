/**
 * Tests for the capacity-over lock banner (#1941), with the real translator
 * so the sentences are the ones a user reads in each locale.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CapacityLockBanner,
  CapacityLockBannerView,
  overageMegabytes,
} from "./CapacityLockBanner";
import type { CapacityLock } from "@/lib/api/usage";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const getUsage = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspaceUsageCurrent: () => getUsage(),
}));
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspaceId: "ws-1" }),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
}));

const MB = 1024 * 1024;

function lock(over: Partial<CapacityLock> = {}): CapacityLock {
  return {
    memory_count: 1012,
    memory_limit: 1000,
    over_memories: 12,
    used_bytes: 0,
    storage_limit_bytes: 100 * MB,
    over_bytes: 0,
    cleanup_url: "https://app.example.test/workspace/settings/plan",
    ...over,
  };
}

function renderIn(locale: "en" | "ja", ui: ReactElement) {
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "en" ? en : ja}
    >
      {ui}
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  getUsage.mockReset();
});

describe("CapacityLockBannerView", () => {
  it("names the memories to remove and what still works", () => {
    renderIn("en", <CapacityLockBannerView lock={lock()} />);
    expect(
      screen.getByText("This workspace is over the Free plan's capacity"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Remove 12 memories, or re-subscribe. Until then, search and saving are paused; listing, deleting and export still work.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View plan" })).toHaveAttribute(
      "href",
      "/workspace/settings/plan",
    );
    expect(
      screen.getByRole("link", { name: "Clean up contexts" }),
    ).toHaveAttribute("href", "/workspace/contexts");
  });

  it("names both axes when both are over", () => {
    renderIn(
      "en",
      <CapacityLockBannerView
        lock={lock({ over_memories: 1, over_bytes: 3 * MB + 1 })}
      />,
    );
    expect(
      screen.getByText(/Remove 1 memory and 3\.1 MB,/),
    ).toBeInTheDocument();
  });

  it("renders Japanese copy", () => {
    renderIn(
      "ja",
      <CapacityLockBannerView
        lock={lock({ over_memories: 0, over_bytes: 2 * MB })}
      />,
    );
    expect(
      screen.getByText(/2 MBを減らすか、再契約してください/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "プランを見る" }),
    ).toBeInTheDocument();
  });
});

describe("CapacityLockBanner", () => {
  it("renders nothing when the workspace is not locked", async () => {
    getUsage.mockResolvedValue({ capacity_lock: null });
    renderIn("en", <CapacityLockBanner />);
    await waitFor(() => expect(getUsage).toHaveBeenCalled());
    expect(screen.queryByTestId("capacity-lock-banner")).toBeNull();
  });

  it("renders nothing when the usage read fails", async () => {
    getUsage.mockRejectedValue(new Error("down"));
    renderIn("en", <CapacityLockBanner />);
    await waitFor(() => expect(getUsage).toHaveBeenCalled());
    expect(screen.queryByTestId("capacity-lock-banner")).toBeNull();
  });

  it("shows the banner when usage reports a lock", async () => {
    getUsage.mockResolvedValue({ capacity_lock: lock() });
    renderIn("en", <CapacityLockBanner />);
    expect(
      await screen.findByTestId("capacity-lock-banner"),
    ).toBeInTheDocument();
  });
});

describe("CapacityLockBanner refresh", () => {
  it("re-reads usage when a delete asks for it, and clears once unlocked", async () => {
    const { requestCapacityRefresh } = await import("@/lib/capacityRefresh");
    getUsage
      .mockResolvedValueOnce({ capacity_lock: lock() })
      .mockResolvedValueOnce({ capacity_lock: null });
    renderIn("en", <CapacityLockBanner />);
    expect(
      await screen.findByTestId("capacity-lock-banner"),
    ).toBeInTheDocument();
    act(() => requestCapacityRefresh());
    await waitFor(() =>
      expect(screen.queryByTestId("capacity-lock-banner")).toBeNull(),
    );
    expect(getUsage).toHaveBeenCalledTimes(2);
  });
});

describe("overageMegabytes", () => {
  it("rounds up to a tenth so removing that much is enough", () => {
    expect(overageMegabytes(1)).toBe("0.1");
    expect(overageMegabytes(5 * MB)).toBe("5");
    expect(overageMegabytes(5 * MB + 1)).toBe("5.1");
  });
});
