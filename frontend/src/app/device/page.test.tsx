/**
 * Tests for Device Authorization page (Issue #536).
 *
 * Covers: code input, auto-verify from URL param, consent approve/deny,
 * and terminal states (success, denied, error, expired).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ApiError } from "@/lib/api";
import DevicePage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockVerifyDeviceCode = vi.fn();
const mockConfirmDevice = vi.fn();

vi.mock("@/lib/auth/auth", () => ({
  getAuthUrl: vi.fn(),
  getGitHubAuthUrl: vi.fn(),
  getAuthConfig: vi.fn(),
  loginWithPassword: vi.fn(),
  verifyMfa: vi.fn(),
  verifyDeviceCode: (...args: unknown[]) => mockVerifyDeviceCode(...args),
  confirmDevice: (...args: unknown[]) => mockConfirmDevice(...args),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string, params?: Record<string, string>) => {
    if (params?.scope) return k.replace("{scope}", params.scope);
    return k;
  },
}));

const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
  useSearchParams: () => mockSearchParams,
}));

// ---------- Helpers ----------------------------------------------------------

function typeCode(code: string) {
  const input = screen.getByLabelText("device.codeLabel");
  for (const char of code) {
    fireEvent.change(input, {
      target: { value: input.getAttribute("value") + char },
    });
  }
  return input;
}

beforeEach(() => {
  mockVerifyDeviceCode.mockReset();
  mockConfirmDevice.mockReset();
  // Clear URL params between tests
  for (const key of [...mockSearchParams.keys()]) {
    mockSearchParams.delete(key);
  }
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ---------- Tests ------------------------------------------------------------

describe("DevicePage", () => {
  it("renders code input form", () => {
    render(<DevicePage />);
    expect(screen.getByText("device.title")).toBeDefined();
    expect(screen.getByText("device.description")).toBeDefined();
    expect(screen.getByLabelText("device.codeLabel")).toBeDefined();
  });

  it("shows verifying state while checking code", async () => {
    mockVerifyDeviceCode.mockImplementation(
      () => new Promise(() => {}), // never resolves
    );
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "ABCD1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.verifying")).toBeDefined();
    });
  });

  it("shows consent screen with client info", async () => {
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "ABCD1234",
      client_name: "Test CLI",
      scope: "memory:read memory:write",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: false,
      is_expired: false,
    });
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "ABCD1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.consentTitle")).toBeDefined();
      expect(screen.getByText("Test CLI")).toBeDefined();
      expect(screen.getByText("device.approve")).toBeDefined();
      expect(screen.getByText("device.deny")).toBeDefined();
    });
  });

  it("shows success screen after approve", async () => {
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "ABCD1234",
      client_name: "Test CLI",
      scope: "memory:read",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: false,
      is_expired: false,
    });
    mockConfirmDevice.mockResolvedValue({
      status: "approved",
      user_code: "ABCD1234",
    });
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "ABCD1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.approve")).toBeDefined();
    });
    fireEvent.click(screen.getByText("device.approve"));

    await waitFor(() => {
      expect(screen.getByText("device.successTitle")).toBeDefined();
      expect(screen.getByText("device.successMessage")).toBeDefined();
    });
  });

  it("shows denied screen after deny", async () => {
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "ABCD1234",
      client_name: "Test CLI",
      scope: "memory:read",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: false,
      is_expired: false,
    });
    mockConfirmDevice.mockResolvedValue({
      status: "denied",
      user_code: "ABCD1234",
    });
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "ABCD1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.deny")).toBeDefined();
    });
    fireEvent.click(screen.getByText("device.deny"));

    await waitFor(() => {
      expect(screen.getByText("device.deniedTitle")).toBeDefined();
      expect(screen.getByText("device.deniedMessage")).toBeDefined();
    });
  });

  it("shows error when code is invalid", async () => {
    mockVerifyDeviceCode.mockRejectedValue(new Error("not found"));
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "XXXXXXXX" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.invalidCode")).toBeDefined();
    });
  });

  it("auto-verifies from URL user_code param", async () => {
    mockSearchParams.set("user_code", "URL12345");
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "URL12345",
      client_name: "URL Client",
      scope: "memory:read",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: false,
      is_expired: false,
    });
    render(<DevicePage />);

    await waitFor(() => {
      expect(screen.getByText("URL Client")).toBeDefined();
    });
  });

  it("shows success immediately for already-authorized code", async () => {
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "AUTH1234",
      client_name: "Test CLI",
      scope: "memory:read",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: true,
      is_expired: false,
    });
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "AUTH1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.successTitle")).toBeDefined();
    });
  });

  it("transitions to success on 409 already-authorized during confirm", async () => {
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "ABCD1234",
      client_name: "Test CLI",
      scope: "memory:read",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: false,
      is_expired: false,
    });
    mockConfirmDevice.mockRejectedValue(
      new ApiError({
        message: "This code has already been authorized",
        status: 409,
      }),
    );
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "ABCD1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.approve")).toBeDefined();
    });
    fireEvent.click(screen.getByText("device.approve"));

    await waitFor(() => {
      expect(screen.getByText("device.successTitle")).toBeDefined();
    });
  });

  it("transitions to denied on 409 already-denied during confirm", async () => {
    mockVerifyDeviceCode.mockResolvedValue({
      user_code: "ABCD1234",
      client_name: "Test CLI",
      scope: "memory:read",
      expires_at: "2026-01-01T00:00:00Z",
      is_authorized: false,
      is_expired: false,
    });
    mockConfirmDevice.mockRejectedValue(
      new ApiError({
        message: "This code has already been denied",
        status: 409,
      }),
    );
    render(<DevicePage />);

    const input = screen.getByLabelText("device.codeLabel");
    fireEvent.change(input, { target: { value: "ABCD1234" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("device.deny")).toBeDefined();
    });
    fireEvent.click(screen.getByText("device.deny"));

    await waitFor(() => {
      expect(screen.getByText("device.deniedTitle")).toBeDefined();
    });
  });
});
