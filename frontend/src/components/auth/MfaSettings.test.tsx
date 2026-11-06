import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MfaSettings } from "./MfaSettings";

const { user, post, refetchUser } = vi.hoisted(() => ({
  user: { has_password: true, mfa_enabled: false, mfa_recovery_codes_remaining: 3 }, post: vi.fn(), refetchUser: vi.fn(),
}));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ user, refetchUser }) }));
vi.mock("@/lib/api/base", () => ({ apiClient: { post } }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));

beforeEach(() => {
  user.has_password = true;
  user.mfa_enabled = false;
  post.mockReset();
  refetchUser.mockReset();
});

describe("MfaSettings", () => {
  it("hides enrollment for an account without a password", () => {
    user.has_password = false;
    const { container } = render(<MfaSettings />);
    expect(container).toBeEmptyDOMElement();
  });

  it("confirms the local QR, then shows recovery codes once", async () => {
    post.mockResolvedValueOnce({ secret: "manual-key", provisioning_uri: "otpauth://totp/test?secret=test" })
      .mockResolvedValueOnce({ recovery_codes: ["save-once"] });
    render(<MfaSettings />);
    fireEvent.click(screen.getByRole("button", { name: "setup" }));
    const dialog = within(screen.getByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("password"), { target: { value: "current" } });
    fireEvent.click(dialog.getByRole("button", { name: "setup" }));
    expect(await dialog.findByText("manual-key")).toBeInTheDocument();
    const input = dialog.getByLabelText("code");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).not.toHaveAttribute("maxlength");
    fireEvent.change(input, { target: { value: "　１２３ ４５６" } });
    fireEvent.click(dialog.getByRole("button", { name: "enable" }));
    expect(await dialog.findByText("save-once")).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith("/api/v1/auth/mfa/setup", { current_password: "current" });
    expect(post).toHaveBeenCalledWith("/api/v1/auth/mfa/enable", { code: "123456" });
    fireEvent.click(dialog.getByRole("button", { name: "saved" }));
    expect(screen.queryByText("save-once")).toBeNull();
  });

  it("shows the remaining count and regenerates after fresh proof", async () => {
    user.mfa_enabled = true;
    post.mockResolvedValueOnce({ recovery_codes: ["replacement"] });
    render(<MfaSettings />);
    expect(screen.getByText("remaining")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "regenerate" }));
    const dialog = within(screen.getByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("password"), { target: { value: "fresh" } });
    fireEvent.change(dialog.getByLabelText("code"), { target: { value: "１２３　４５６" } });
    fireEvent.click(dialog.getByRole("button", { name: "regenerate" }));
    expect(await dialog.findByText("replacement")).toBeVisible();
    expect(post).toHaveBeenCalledWith("/api/v1/auth/mfa/recovery-codes/regenerate", {current_password: "fresh", code: "123456"});
  });

  it("keeps a failed removal inside the dialog", async () => {
    user.mfa_enabled = true;
    post.mockRejectedValueOnce(new Error("Invalid code"));
    render(<MfaSettings />);
    fireEvent.click(screen.getByRole("button", { name: "disable" }));
    const dialog = within(screen.getByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("password"), { target: { value: "current" } });
    const input = dialog.getByLabelText("codeOrRecovery");
    fireEvent.change(input, { target: { value: "１２３４５" } });
    expect(dialog.getByRole("button", { name: "disable" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "　１２３　４５６ " } });
    fireEvent.click(dialog.getByRole("button", { name: "disable" }));
    expect(await dialog.findByRole("alert")).toHaveTextContent("Invalid code");
    expect(post).toHaveBeenCalledWith("/api/v1/auth/mfa/disable", { current_password: "current", code: "123456" });
  });
});
