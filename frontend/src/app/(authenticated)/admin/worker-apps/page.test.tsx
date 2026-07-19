import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import WorkerAppsPage from "./page";

const mockListWorkerApps = vi.fn();
const mockCreateWorkerApp = vi.fn();
const mockUpdateWorkerApp = vi.fn();
const mockRotateWorkerAppSecret = vi.fn();
vi.mock("@/lib/api/worker-apps", () => ({
  listWorkerApps: (...args: unknown[]) => mockListWorkerApps(...args),
  createWorkerApp: (...args: unknown[]) => mockCreateWorkerApp(...args),
  updateWorkerApp: (...args: unknown[]) => mockUpdateWorkerApp(...args),
  rotateWorkerAppSecret: (...args: unknown[]) =>
    mockRotateWorkerAppSecret(...args),
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

beforeEach(() => {
  mockListWorkerApps.mockResolvedValue([]);
  mockCreateWorkerApp.mockResolvedValue({});
  mockUpdateWorkerApp.mockResolvedValue({});
  mockRotateWorkerAppSecret.mockResolvedValue({});
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WorkerAppsPage", () => {
  it("loads lifecycle controls for a system admin", async () => {
    mockUseAuth.mockReturnValue({
      user: { role: "admin" },
      isLoading: false,
    });

    render(<WorkerAppsPage />);

    expect(await screen.findByText("emptyTitle")).toBeInTheDocument();
    await waitFor(() => expect(mockListWorkerApps).toHaveBeenCalledOnce());
    expect(screen.getByText("create")).toBeInTheDocument();
  });

  it("fails closed in the UI for a non-system-admin", async () => {
    mockUseAuth.mockReturnValue({
      user: { role: "user" },
      isLoading: false,
    });

    render(<WorkerAppsPage />);

    expect(await screen.findByText("forbidden")).toBeInTheDocument();
    expect(mockListWorkerApps).not.toHaveBeenCalled();
  });

  it("creates an identity and clears the submitted signing secret", async () => {
    mockUseAuth.mockReturnValue({
      user: { role: "admin" },
      isLoading: false,
    });

    render(<WorkerAppsPage />);
    await screen.findByText("emptyTitle");

    fireEvent.change(screen.getByLabelText("appKey"), {
      target: { value: "sales" },
    });
    fireEvent.change(screen.getByLabelText("displayName"), {
      target: { value: "Sales Slack App" },
    });
    const secretInput = screen.getByLabelText("signingSecret");
    fireEvent.change(secretInput, { target: { value: "signing-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "create" }));

    await waitFor(() =>
      expect(mockCreateWorkerApp).toHaveBeenCalledWith({
        platform: "slack",
        app_key: "sales",
        display_name: "Sales Slack App",
        signing_secret: "signing-secret",
      }),
    );
    await waitFor(() => expect(secretInput).toHaveValue(""));
  });

  it("rotates and disables an active identity without rendering secret material", async () => {
    const app = {
      platform: "slack",
      app_key: "sales",
      display_name: "Sales Slack App",
      status: "active",
      revision: "opaque-revision",
      has_active_secret: true,
      active_secret_revision: 2,
      retiring_secret_revision: 1,
      retiring_valid_until: "2026-07-17T10:00:00Z",
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    };
    mockUseAuth.mockReturnValue({
      user: { role: "admin" },
      isLoading: false,
    });
    mockListWorkerApps.mockResolvedValue([app]);

    render(<WorkerAppsPage />);
    await screen.findByText("sales");

    const rotationInput = screen.getByLabelText("newSigningSecretFor");
    fireEvent.change(rotationInput, { target: { value: "next-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "rotate" }));

    await waitFor(() =>
      expect(mockRotateWorkerAppSecret).toHaveBeenCalledWith(
        expect.objectContaining({ app_key: "sales", platform: "slack" }),
        "next-secret",
      ),
    );
    await waitFor(() => expect(rotationInput).toHaveValue(""));
    expect(screen.queryByText("next-secret")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "disable" }));
    await waitFor(() =>
      expect(mockUpdateWorkerApp).toHaveBeenCalledWith(
        expect.objectContaining({ app_key: "sales", platform: "slack" }),
        { status: "disabled" },
      ),
    );
  });
it("surfaces action failures as a destructive toast (#1360)", async () => {
    const app = {
      platform: "slack",
      app_key: "sales",
      display_name: "Sales Slack App",
      status: "active",
      revision: "opaque-revision",
      has_active_secret: true,
      active_secret_revision: 2,
      retiring_secret_revision: null,
      retiring_valid_until: null,
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    };
    mockUseAuth.mockReturnValue({ user: { role: "admin" }, isLoading: false });
    mockListWorkerApps.mockResolvedValue([app]);
    mockUpdateWorkerApp.mockRejectedValue(new Error("boom"));

    render(<WorkerAppsPage />);
    await screen.findByText("sales");

    fireEvent.click(screen.getByRole("button", { name: "save" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          title: "operationFailed",
          description: "boom",
        }),
      ),
    );
    // No inline banner for an action failure (error-surface rule).
    expect(screen.queryByText("boom")).not.toBeInTheDocument();
  });

  it("keeps another row's unsaved draft across a reload (#1360)", async () => {
    const base = {
      platform: "slack",
      status: "active",
      revision: "r",
      has_active_secret: true,
      active_secret_revision: 1,
      retiring_secret_revision: null,
      retiring_valid_until: null,
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    };
    const appA = { ...base, app_key: "alpha", display_name: "Alpha" };
    const appB = { ...base, app_key: "beta", display_name: "Beta" };
    mockUseAuth.mockReturnValue({ user: { role: "admin" }, isLoading: false });
    mockListWorkerApps.mockResolvedValue([appA, appB]);

    render(<WorkerAppsPage />);
    await screen.findByText("alpha");

    const inputs = screen.getAllByLabelText("displayNameFor");
    // Draft an edit in row B, then save row A.
    fireEvent.change(inputs[1], { target: { value: "Beta DRAFT" } });
    fireEvent.click(screen.getAllByRole("button", { name: "save" })[0]);

    await waitFor(() =>
      expect(mockUpdateWorkerApp).toHaveBeenCalledWith(
        expect.objectContaining({ app_key: "alpha" }),
        { display_name: "Alpha" },
      ),
    );
    // The reload after the save must NOT discard row B's draft.
    await waitFor(() =>
      expect(mockListWorkerApps).toHaveBeenCalledTimes(2),
    );
    expect(screen.getAllByLabelText("displayNameFor")[1]).toHaveValue(
      "Beta DRAFT",
    );
  });
});
