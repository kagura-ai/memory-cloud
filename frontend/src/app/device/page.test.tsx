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

const mockRouterReplace = vi.fn();
const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockRouterReplace, push: vi.fn() }),
  useSearchParams: () => mockSearchParams,
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockApiClientPost = vi.fn();

vi.mock("@/lib/api/base", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/base")>("@/lib/api/base");
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: (...args: unknown[]) => mockApiClientPost(...args),
    },
  };
});

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
  mockRouterReplace.mockReset();
  // Default: authenticated user. Override per-test with mockUseAuth.mockReturnValue({...}).
  mockUseAuth.mockReturnValue({
    user: {
      id: "u1",
      email: "test@example.com",
      name: "Test",
      picture: "",
      role: "user" as const,
    },
    isLoading: false,
    isAuthenticated: true,
    logout: vi.fn(),
    refetchUser: vi.fn(),
  });
  // Clear URL params between tests
  for (const key of [...mockSearchParams.keys()]) {
    mockSearchParams.delete(key);
  }
  mockApiClientPost.mockReset();
  mockApiClientPost.mockResolvedValue(undefined);
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

  // --- Auth guard tests (#772) ---

  describe("auth guard", () => {
    it("(a) redirects to login with encoded return_to when unauthenticated and user_code is present", async () => {
      mockUseAuth.mockReturnValue({ user: null, isLoading: false });
      mockSearchParams.set("user_code", "ABC12345");

      render(<DevicePage />);

      await waitFor(() => {
        expect(mockRouterReplace).toHaveBeenCalledWith(
          "/login?return_to=%2Fdevice%3Fuser_code%3DABC12345",
        );
      });
      expect(mockVerifyDeviceCode).not.toHaveBeenCalled();
    });

    it("(b) redirects to login with bare /device return_to when unauthenticated and no user_code", async () => {
      mockUseAuth.mockReturnValue({ user: null, isLoading: false });
      // No user_code set in mockSearchParams (cleared in beforeEach)

      render(<DevicePage />);

      await waitFor(() => {
        expect(mockRouterReplace).toHaveBeenCalledWith(
          "/login?return_to=%2Fdevice",
        );
      });
      expect(mockVerifyDeviceCode).not.toHaveBeenCalled();
    });

    it("(c) shows spinner and does not redirect or verify while auth is resolving", async () => {
      mockUseAuth.mockReturnValue({ user: null, isLoading: true });

      const { container } = render(<DevicePage />);

      // SpinnerLoading renders a div with the `animate-spin` CSS class — assert
      // it is actually present so the test fails if the component returns null
      // instead of a spinner (negative assertion alone would not catch that).
      expect(container.querySelector('[class*="animate-spin"]')).not.toBeNull();
      // The code input form must be absent while auth is resolving.
      expect(screen.queryByLabelText("device.codeLabel")).toBeNull();
      expect(mockVerifyDeviceCode).not.toHaveBeenCalled();
      expect(mockRouterReplace).not.toHaveBeenCalled();
    });

    it("(d) authed user sees the code input form and verify works normally", async () => {
      // Default beforeEach already sets authenticated user — confirm behavior preserved.
      render(<DevicePage />);

      expect(screen.getByLabelText("device.codeLabel")).toBeDefined();
      expect(mockRouterReplace).not.toHaveBeenCalled();
    });
  });

  it("discloses identity fields shared with the client on consent screen (#640)", async () => {
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
      expect(screen.getByText("device.identityShareLabel")).toBeDefined();
      expect(screen.getByText("device.identityEmail")).toBeDefined();
      expect(screen.getByText("device.identityWorkspace")).toBeDefined();
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

describe("device page audit ping on unauth (#779)", () => {
  it("fires POST /api/v1/oauth/device/audit-unauth with prefix before redirecting unauth user", async () => {
    mockUseAuth.mockReturnValue({
      user: null,
      isLoading: false,
      isAuthenticated: false,
    });
    mockSearchParams.set("user_code", "ABCD1234");

    render(<DevicePage />);

    await waitFor(() => {
      expect(mockApiClientPost).toHaveBeenCalledWith(
        "/api/v1/oauth/device/audit-unauth",
        { user_code_prefix: "ABCD" },
      );
    });
    expect(mockRouterReplace).toHaveBeenCalledWith(
      "/login?return_to=%2Fdevice%3Fuser_code%3DABCD1234",
    );

    mockSearchParams.delete("user_code");
  });

  it("sends empty user_code_prefix when no code in URL", async () => {
    mockUseAuth.mockReturnValue({
      user: null,
      isLoading: false,
      isAuthenticated: false,
    });
    mockSearchParams.delete("user_code");

    render(<DevicePage />);

    await waitFor(() => {
      expect(mockApiClientPost).toHaveBeenCalledWith(
        "/api/v1/oauth/device/audit-unauth",
        { user_code_prefix: "" },
      );
    });
  });

  it("still redirects even if audit ping rejects (fire-and-forget)", async () => {
    mockUseAuth.mockReturnValue({
      user: null,
      isLoading: false,
      isAuthenticated: false,
    });
    mockApiClientPost.mockRejectedValueOnce(new Error("network down"));
    mockSearchParams.set("user_code", "WXYZ5678");

    render(<DevicePage />);

    await waitFor(() => {
      expect(mockRouterReplace).toHaveBeenCalledWith(
        "/login?return_to=%2Fdevice%3Fuser_code%3DWXYZ5678",
      );
    });

    mockSearchParams.delete("user_code");
  });

  it("normalizes user_code_prefix: uppercases and strips non-alphanumeric characters", async () => {
    // user_code is uppercase alphanumeric (RFC 8628). A malformed/attacker-supplied
    // URL like `/device?user_code=ab<cd&ef` must not write garbage to audit logs.
    mockUseAuth.mockReturnValue({
      user: null,
      isLoading: false,
      isAuthenticated: false,
    });
    mockSearchParams.set("user_code", "ab<cd&ef");

    render(<DevicePage />);

    await waitFor(() => {
      expect(mockApiClientPost).toHaveBeenCalledWith(
        "/api/v1/oauth/device/audit-unauth",
        { user_code_prefix: "ABCD" },
      );
    });

    mockSearchParams.delete("user_code");
  });
});
