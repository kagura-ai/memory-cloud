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
});
