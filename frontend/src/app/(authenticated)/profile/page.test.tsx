/**
 * Tests for the profile page sign-in-method display (Issue #514).
 *
 * Covers:
 *   - getSignInMethodLabel: 4 branches (google, github, password, legacy-null)
 *   - render: the new "Sign-in method" Input shows the right label per user shape
 *   - render: password-auth user is NOT routed to a provider label
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import ProfilePage, { getSignInMethodLabel } from "./page";

// ---------- Mocks ------------------------------------------------------------

const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
}));

// AuthContext: render returns a user with the auth_method/auth_provider shape
// the test sets via the helper below.
let mockUser: {
  id: string;
  email: string;
  name: string;
  role?: string;
  timezone?: string;
  auth_method?: "password" | "oauth";
  auth_provider?: "google" | "github" | null;
} | null = null;

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: mockUser,
    refetchUser: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/lib/api/base", () => ({
  apiClient: {
    put: vi.fn(),
  },
}));

// ---------- getSignInMethodLabel unit tests ---------------------------------

describe("getSignInMethodLabel", () => {
  it("returns Google label for OAuth + google provider", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "google" },
        stableTranslator,
      ),
    ).toBe("signInMethodGoogle");
  });

  it("returns GitHub label for OAuth + github provider", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: "github" },
        stableTranslator,
      ),
    ).toBe("signInMethodGitHub");
  });

  it("returns Password label for password auth_method (provider is ignored)", () => {
    expect(
      getSignInMethodLabel(
        { auth_method: "password", auth_provider: null },
        stableTranslator,
      ),
    ).toBe("signInMethodPassword");
  });

  it("returns Other label for legacy OAuth user with null provider", () => {
    // Pre-#361 users may have auth_method='oauth' AND auth_provider=null.
    // We surface "Other" rather than guessing a provider.
    expect(
      getSignInMethodLabel(
        { auth_method: "oauth", auth_provider: null },
        stableTranslator,
      ),
    ).toBe("signInMethodOther");
  });

  it("returns Other label when both fields are undefined (defensive)", () => {
    expect(getSignInMethodLabel({}, stableTranslator)).toBe(
      "signInMethodOther",
    );
  });
});

// ---------- ProfilePage render tests ----------------------------------------

describe("ProfilePage — sign-in method section", () => {
  it("shows the Google label for an OAuth + google user", () => {
    mockUser = {
      id: "u-1",
      email: "u@example.com",
      name: "Test",
      role: "user",
      timezone: "UTC",
      auth_method: "oauth",
      auth_provider: "google",
    };

    render(<ProfilePage />);

    const input = screen.getByLabelText("signInMethod") as HTMLInputElement;
    expect(input.value).toBe("signInMethodGoogle");
    expect(input.disabled).toBe(true);
  });

  it("shows the GitHub label for an OAuth + github user", () => {
    mockUser = {
      id: "u-2",
      email: "u@example.com",
      name: "Test",
      role: "user",
      timezone: "UTC",
      auth_method: "oauth",
      auth_provider: "github",
    };

    render(<ProfilePage />);

    const input = screen.getByLabelText("signInMethod") as HTMLInputElement;
    expect(input.value).toBe("signInMethodGitHub");
  });

  it("shows the Password label for a password-auth user", () => {
    mockUser = {
      id: "u-3",
      email: "u@example.com",
      name: "Test",
      role: "user",
      timezone: "UTC",
      auth_method: "password",
      auth_provider: null,
    };

    render(<ProfilePage />);

    const input = screen.getByLabelText("signInMethod") as HTMLInputElement;
    expect(input.value).toBe("signInMethodPassword");
  });

  it("shows the Other label for a legacy OAuth user without a provider", () => {
    mockUser = {
      id: "u-4",
      email: "u@example.com",
      name: "Test",
      role: "user",
      timezone: "UTC",
      auth_method: "oauth",
      auth_provider: null,
    };

    render(<ProfilePage />);

    const input = screen.getByLabelText("signInMethod") as HTMLInputElement;
    expect(input.value).toBe("signInMethodOther");
  });
});
