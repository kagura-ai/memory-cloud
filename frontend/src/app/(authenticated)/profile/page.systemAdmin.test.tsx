/**
 * The System Administrator field follows the deployment's system-admin surface (#2767).
 *
 * `role === "admin"` grants something only where `features.system_admin` is
 * true (SYSTEM_ADMIN_MODE=session); elsewhere the profile does not claim it.
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

let mockUser: Record<string, unknown> | null = null;
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockUser, refetchUser: vi.fn() }),
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

const admin = { id: "u-1", name: "Admin", email: "admin@example.test", role: "admin", auth_method: "oauth", auth_provider: "google" };

describe("System Administrator field (#2767)", () => {
  beforeEach(() => {
    mockUser = { ...admin };
    mockFeatures = null;
  });

  it("is shown for an admin where the deployment serves the admin surface", () => {
    mockFeatures = { system_admin: true };
    render(<ProfilePage />);
    expect(screen.getByLabelText("systemRole")).toHaveValue("systemAdmin");
  });

  it.each([
    ["off", { system_admin: false }],
    ["absent", {}],
  ])("is hidden for an admin when features.system_admin is %s", (_label, features) => {
    mockFeatures = features;
    render(<ProfilePage />);
    expect(screen.queryByLabelText("systemRole")).toBeNull();
  });

  it("is hidden while the deployment flags are unknown", () => {
    render(<ProfilePage />);
    expect(screen.queryByLabelText("systemRole")).toBeNull();
  });

  it("is hidden for a non-admin even where the surface is served", () => {
    mockUser = { ...admin, role: "user" };
    mockFeatures = { system_admin: true };
    render(<ProfilePage />);
    expect(screen.queryByLabelText("systemRole")).toBeNull();
  });
});
