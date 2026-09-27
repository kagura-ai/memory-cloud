/**
 * Profile "Password" section (#1678): set-up request for a passwordless
 * account; change + remove for an account with a password; the last-method
 * guard; the error channels (field / dialog Alert / toast).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Stable translator (a fresh function per render re-runs the load effect).
const stableT = (key: string, values?: Record<string, unknown>) =>
  values && "email" in values ? `${key}|${values.email}` : key;
vi.mock("next-intl", () => ({ useTranslations: () => stableT }));

let mockUser: {
  email: string;
  auth_method?: "password" | "oauth";
  has_password?: boolean;
} | null = null;
const mockRefetchUser = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockUser, refetchUser: mockRefetchUser }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const { mockGet, FakeApiError, mockSetupRequest, mockChange, mockRemove } =
  vi.hoisted(() => {
    class FakeApiError extends Error {
      readonly status: number;
      constructor(status: number) {
        super("fake");
        this.status = status;
      }
    }
    return {
      mockGet: vi.fn(),
      FakeApiError,
      mockSetupRequest: vi.fn(),
      mockChange: vi.fn(),
      mockRemove: vi.fn(),
    };
  });
vi.mock("@/lib/api/base", () => ({
  apiClient: { get: (...a: unknown[]) => mockGet(...a) },
  ApiError: FakeApiError,
}));
vi.mock("@/lib/auth/auth", () => ({
  requestPasswordSetup: (...a: unknown[]) => mockSetupRequest(...a),
  changePassword: (...a: unknown[]) => mockChange(...a),
  removePassword: (...a: unknown[]) => mockRemove(...a),
}));

import PasswordSettings from "./PasswordSettings";

function providers(...names: string[]) {
  mockGet.mockResolvedValue({
    providers: names.map((provider) => ({ provider })),
  });
}

beforeEach(() => {
  mockUser = {
    email: "me@example.com",
    auth_method: "oauth",
    has_password: false,
  };
  mockRefetchUser.mockReset();
  mockToast.mockReset();
  mockGet.mockReset();
  mockSetupRequest.mockReset();
  mockChange.mockReset();
  mockRemove.mockReset();
});

describe("without a password", () => {
  it("requests a set-up link and says where it went", async () => {
    providers("google");
    mockSetupRequest.mockResolvedValue(undefined);
    render(<PasswordSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "setupButton" }));

    await waitFor(() =>
      expect(screen.getByText("setupSent|me@example.com")).toBeTruthy(),
    );
    expect(screen.getByText("statusNotSet")).toBeTruthy();
    expect(screen.queryByLabelText("currentPassword")).toBeNull();
  });

  it("toasts a failed send", async () => {
    providers("google");
    mockSetupRequest.mockRejectedValue(new FakeApiError(503));
    render(<PasswordSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "setupButton" }));

    await waitFor(() =>
      expect(mockToast.mock.calls[0][0]).toMatchObject({
        description: "setupError",
        variant: "destructive",
      }),
    );
  });

  it("refreshes the user on 409 (a password was set elsewhere)", async () => {
    providers("google");
    mockSetupRequest.mockRejectedValue(new FakeApiError(409));
    render(<PasswordSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "setupButton" }));

    await waitFor(() => expect(mockRefetchUser).toHaveBeenCalled());
    expect(mockToast).not.toHaveBeenCalled();
  });
});

describe("with a password", () => {
  beforeEach(() => {
    mockUser = {
      email: "me@example.com",
      auth_method: "oauth",
      has_password: true,
    };
  });

  async function fillChange(current: string, next: string, confirm: string) {
    fireEvent.change(await screen.findByLabelText("currentPassword"), {
      target: { value: current },
    });
    fireEvent.change(screen.getByLabelText("newPassword"), {
      target: { value: next },
    });
    fireEvent.change(screen.getByLabelText("confirmPassword"), {
      target: { value: confirm },
    });
    fireEvent.click(screen.getByRole("button", { name: "changeButton" }));
  }

  it("changes the password", async () => {
    providers("github");
    mockChange.mockResolvedValue(undefined);
    render(<PasswordSettings />);

    await fillChange("Old-Pass-123!", "New-Pass-456!", "New-Pass-456!");

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith({ title: "changeSuccess" }),
    );
    expect(mockChange).toHaveBeenCalledWith("Old-Pass-123!", "New-Pass-456!");
  });

  it("shows a wrong current password next to the field", async () => {
    providers("github");
    mockChange.mockRejectedValue(new FakeApiError(403));
    render(<PasswordSettings />);

    await fillChange("Wrong", "New-Pass-456!", "New-Pass-456!");

    await waitFor(() => expect(screen.getByText("wrongCurrent")).toBeTruthy());
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("refuses a mismatch locally", async () => {
    providers("github");
    render(<PasswordSettings />);

    await fillChange("Old-Pass-123!", "New-Pass-456!", "Other-Pass-789!");

    expect(screen.getByText("mismatch")).toBeTruthy();
    expect(mockChange).not.toHaveBeenCalled();
  });

  it("disables removal when no OAuth provider is linked", async () => {
    providers();
    render(<PasswordSettings />);

    const remove = await screen.findByRole("button", { name: "removeButton" });
    expect((remove as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("removeDisabledHint")).toBeTruthy();
  });

  it("re-reads the linked providers when Connected Accounts changes them", async () => {
    providers();
    const { rerender } = render(<PasswordSettings providersVersion={0} />);
    const remove = await screen.findByRole("button", { name: "removeButton" });
    expect((remove as HTMLButtonElement).disabled).toBe(true);

    // A provider was linked elsewhere on the page.
    providers("github");
    rerender(<PasswordSettings providersVersion={1} />);

    await waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: "removeButton",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false),
    );
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it("removes the password after confirming the current one", async () => {
    providers("google");
    mockRemove.mockResolvedValue(undefined);
    render(<PasswordSettings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "removeButton" }),
    );
    fireEvent.change(screen.getAllByLabelText("currentPassword").at(-1)!, {
      target: { value: "Old-Pass-123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "removeConfirm" }));

    await waitFor(() =>
      expect(mockRemove).toHaveBeenCalledWith("Old-Pass-123!"),
    );
    await waitFor(() => expect(mockRefetchUser).toHaveBeenCalled());
    expect(mockToast).toHaveBeenCalledWith({ title: "removeSuccess" });
  });

  it("shows the last-method refusal inside the dialog", async () => {
    providers("google");
    mockRemove.mockRejectedValue(new FakeApiError(409));
    render(<PasswordSettings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "removeButton" }),
    );
    fireEvent.change(screen.getAllByLabelText("currentPassword").at(-1)!, {
      target: { value: "Old-Pass-123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "removeConfirm" }));

    await waitFor(() =>
      expect(screen.getByText("lastMethodError")).toBeTruthy(),
    );
    expect(mockToast).not.toHaveBeenCalled();
  });
});

describe("older backend without has_password", () => {
  it("treats auth_method=password as having one", async () => {
    mockUser = { email: "admin@example.com", auth_method: "password" };
    providers();
    render(<PasswordSettings />);
    expect(await screen.findByText("statusSet")).toBeTruthy();
  });
});

describe("load failure", () => {
  it("shows a banner and fails closed on removal", async () => {
    mockUser = { email: "me@example.com", has_password: true };
    mockGet.mockRejectedValue(new Error("down"));
    render(<PasswordSettings />);
    expect(await screen.findByText("loadError")).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "removeButton",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
});
