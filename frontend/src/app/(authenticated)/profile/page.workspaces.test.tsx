/**
 * Account settings links to Workspace management (#2919) only where the
 * deployment serves it (`features.workspace_slots`).
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ProfilePage from "./page";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));
vi.mock("@/components/account/DeleteAccountSection", () => ({ DeleteAccountSection: () => null }));
vi.mock("@/components/auth/PasswordSettings", () => ({ default: () => null }));
vi.mock("@/components/auth/LinkedAccounts", () => ({ default: () => null }));
vi.mock("@/components/auth/ConnectedAccounts", () => ({ default: () => null }));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: { id: "u-1", name: "Owner", email: "owner@example.test", auth_method: "password" },
    refetchUser: vi.fn(),
  }),
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

let mockFeatures: Record<string, boolean> | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
  useSystemInfo: () => (mockFeatures ? { features: mockFeatures } : null),
  isSystemInfoUnavailable: () => false,
}));

vi.mock("@/lib/api/base", () => {
  class ApiError extends Error {
    status = 500;
  }
  return {
    updateUserProfile: vi.fn(),
    apiClient: { get: vi.fn().mockRejectedValue(new ApiError()), post: vi.fn(), put: vi.fn() },
    ApiError,
  };
});
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

describe("Workspace management link on Account settings (#2919)", () => {
  beforeEach(() => {
    mockFeatures = null;
  });

  it("links to the page and lists the section where the deployment serves it", () => {
    mockFeatures = { workspace_slots: true };
    render(<ProfilePage />);
    expect(screen.getByRole("link", { name: "profileLink.open" })).toHaveAttribute(
      "href",
      "/profile/workspaces",
    );
    expect(screen.getByRole("link", { name: "sections.workspaces" })).toHaveAttribute(
      "href",
      "#profile-workspaces",
    );
    expect(screen.getByRole("region", { name: "sections.workspaces" })).toBeInTheDocument();
  });

  it.each([
    ["off", { workspace_slots: false }],
    ["absent", {}],
    ["unknown", null],
  ])("is absent when the flag is %s", (_label, features) => {
    mockFeatures = features;
    render(<ProfilePage />);
    expect(screen.queryByRole("link", { name: "profileLink.open" })).toBeNull();
    expect(screen.queryByRole("link", { name: "sections.workspaces" })).toBeNull();
    expect(document.getElementById("profile-workspaces")).toBeNull();
  });
});
