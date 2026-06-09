/**
 * Tests for the workspace Storage (file objects) page (Issue #955).
 *
 * Verifies:
 * - table rows render from listFiles()
 * - empty-state renders when the response is empty
 * - the fetch is held until WorkspaceContext hydrates
 * - errors render via ErrorBanner, not toast
 * - viewer role sees download but NOT delete (backend authz parity)
 * - member+ role sees the delete action
 * - delete confirmation calls deleteFile and refetches
 * - download opens the presigned URL
 * - "Load more" bumps the limit and refetches
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  cleanup,
} from "@testing-library/react";

import StoragePage from "./page";
import type { FileObject } from "@/lib/api/files";

// ---------- Mocks ------------------------------------------------------------

const mockListFiles = vi.fn();
const mockGetDownloadUrl = vi.fn();
const mockDeleteFile = vi.fn();

let mockCurrentWorkspace: {
  id?: string;
  current_user_role?: string;
} | null = null;

vi.mock("@/lib/api/files", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/files")>("@/lib/api/files");
  return {
    ...actual,
    listFiles: (...args: unknown[]) => mockListFiles(...args),
    getDownloadUrl: (...args: unknown[]) => mockGetDownloadUrl(...args),
    deleteFile: (...args: unknown[]) => mockDeleteFile(...args),
  };
});

// Stable translator per-namespace — a fresh fn each render invalidates
// useCallback([t]) and turns the fetch effect into a re-render loop.
const translatorCache = new Map<string, (k: string) => string>();
vi.mock("next-intl", () => ({
  useTranslations: (ns?: string) => {
    const key = ns ?? "";
    if (!translatorCache.has(key)) {
      translatorCache.set(key, (k: string) => k);
    }
    return translatorCache.get(key)!;
  },
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: mockCurrentWorkspace,
    currentWorkspaceId: mockCurrentWorkspace?.id ?? null,
  }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `rel(${iso})`,
  formatDateTime: (iso: string) => `dt(${iso})`,
}));

// ---------- Fixtures ---------------------------------------------------------

const file = (overrides: Partial<FileObject> = {}): FileObject => ({
  id: "file-1",
  workspace_id: "ws-1",
  filename: "report.pdf",
  content_type: "application/pdf",
  size_bytes: 2048,
  sha256: "a".repeat(64),
  status: "uploaded",
  created_at: "2026-03-01T00:00:00Z",
  uploaded_at: "2026-03-01T00:05:00Z",
  ...overrides,
});

beforeEach(() => {
  mockListFiles.mockReset();
  mockGetDownloadUrl.mockReset();
  mockDeleteFile.mockReset();
  mockToast.mockReset();
  mockCurrentWorkspace = { id: "ws-1", current_user_role: "member" };
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("StoragePage", () => {
  it("renders table rows from listFiles()", async () => {
    mockListFiles.mockResolvedValue([
      file(),
      file({ id: "file-2", filename: "notes.txt", content_type: "text/plain" }),
    ]);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText("application/pdf")).toBeInTheDocument();
    expect(mockListFiles).toHaveBeenCalledWith("ws-1", 50);
  });

  it("renders empty state when the list is empty", async () => {
    mockListFiles.mockResolvedValue([]);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("list.emptyTitle")).toBeInTheDocument();
    });
  });

  it("holds the fetch until WorkspaceContext hydrates", async () => {
    mockCurrentWorkspace = null;
    mockListFiles.mockResolvedValue([]);

    render(<StoragePage />);

    await Promise.resolve();
    expect(mockListFiles).not.toHaveBeenCalled();
  });

  it("renders ErrorBanner when fetch rejects", async () => {
    mockListFiles.mockRejectedValue(new Error("backend offline"));

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("backend offline");
    });
    expect(screen.queryByText("list.emptyTitle")).not.toBeInTheDocument();
  });

  it("hides the delete action for viewer role", async () => {
    mockCurrentWorkspace = { id: "ws-1", current_user_role: "viewer" };
    mockListFiles.mockResolvedValue([file()]);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: "list.actions.delete" }),
    ).not.toBeInTheDocument();
    // viewer can still download
    expect(
      screen.getByRole("button", { name: "list.actions.download" }),
    ).toBeInTheDocument();
  });

  it("shows the delete action for member role", async () => {
    mockCurrentWorkspace = { id: "ws-1", current_user_role: "member" };
    mockListFiles.mockResolvedValue([file()]);

    render(<StoragePage />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "list.actions.delete" }),
      ).toBeInTheDocument();
    });
  });

  it("downloads via a presigned URL", async () => {
    mockListFiles.mockResolvedValue([file()]);
    mockGetDownloadUrl.mockResolvedValue("https://r2/presigned");
    // The tab is opened synchronously (about:blank) within the click's user
    // activation, then navigated to the presigned URL once it resolves —
    // this avoids popup blockers that fire on window.open() after an await.
    const fakeTab = {
      location: { replace: vi.fn() },
      opener: {} as unknown,
      close: vi.fn(),
    };
    const openSpy = vi.fn().mockReturnValue(fakeTab);
    vi.stubGlobal("open", openSpy);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.actions.download" }),
    );
    await waitFor(() => {
      expect(mockGetDownloadUrl).toHaveBeenCalledWith("ws-1", "file-1");
    });
    expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
    await waitFor(() => {
      expect(fakeTab.location.replace).toHaveBeenCalledWith(
        "https://r2/presigned",
      );
    });
    expect(fakeTab.opener).toBeNull();
    vi.unstubAllGlobals();
  });

  it("deletes a file after confirmation and refetches", async () => {
    mockListFiles.mockResolvedValueOnce([file()]).mockResolvedValueOnce([]);
    mockDeleteFile.mockResolvedValue(undefined);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.actions.delete" }),
    );

    // Confirm dialog action
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
      ).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
    );

    await waitFor(() => {
      expect(mockDeleteFile).toHaveBeenCalledWith("ws-1", "file-1");
    });
    expect(mockListFiles).toHaveBeenCalledTimes(2);
  });

  it("loads more by bumping the limit when the page is full", async () => {
    // First page returns exactly `limit` rows → "load more" is offered.
    const fullPage = Array.from({ length: 50 }, (_, i) =>
      file({ id: `f${i}`, filename: `file-${i}.txt` }),
    );
    mockListFiles
      .mockResolvedValueOnce(fullPage)
      .mockResolvedValueOnce([...fullPage, file({ id: "f50" })]);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("file-0.txt")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "list.loadMore" }));

    await waitFor(() => {
      expect(mockListFiles).toHaveBeenCalledWith("ws-1", 100);
    });
  });

  it("does not offer load more when the page is not full", async () => {
    // A partial page (< limit) means the backend has nothing more to give.
    mockListFiles.mockResolvedValue([
      file(),
      file({ id: "file-2", filename: "notes.txt" }),
    ]);

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: "list.loadMore" }),
    ).not.toBeInTheDocument();
  });

  it("keeps existing rows visible while loading more (no skeleton wipe)", async () => {
    const fullPage = Array.from({ length: 50 }, (_, i) =>
      file({ id: `f${i}`, filename: `file-${i}.txt` }),
    );
    // Second fetch never resolves during the assertion window.
    let resolveSecond: (v: FileObject[]) => void = () => {};
    mockListFiles.mockResolvedValueOnce(fullPage).mockImplementationOnce(
      () =>
        new Promise<FileObject[]>((res) => {
          resolveSecond = res;
        }),
    );

    render(<StoragePage />);
    await waitFor(() => {
      expect(screen.getByText("file-0.txt")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "list.loadMore" }));

    // While the second page is in flight, the already-loaded rows stay
    // mounted (the page must not fall back to the full skeleton).
    await waitFor(() => {
      expect(mockListFiles).toHaveBeenCalledWith("ws-1", 100);
    });
    expect(screen.getByText("file-0.txt")).toBeInTheDocument();
    resolveSecond(fullPage);
  });

  it("toasts and keeps rows when load-more fails", async () => {
    const fullPage = Array.from({ length: 50 }, (_, i) =>
      file({ id: `f${i}`, filename: `file-${i}.txt` }),
    );
    mockListFiles
      .mockResolvedValueOnce(fullPage)
      .mockRejectedValueOnce(new Error("boom"));

    render(<StoragePage />);
    await waitFor(() => {
      expect(screen.getByText("file-0.txt")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "list.loadMore" }));

    // Load-more is a user action: failures toast (not a page-level banner)
    // and the already-loaded rows stay on screen.
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      );
    });
    expect(screen.getByText("file-0.txt")).toBeInTheDocument();
  });

  it("surfaces a destructive toast when download fails", async () => {
    mockListFiles.mockResolvedValue([file()]);
    mockGetDownloadUrl.mockRejectedValue(new Error("r2 down"));

    render(<StoragePage />);

    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.actions.download" }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      );
    });
    // Download failures use a toast, never the page-level ErrorBanner.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("closes the dialog and toasts on a successful delete", async () => {
    mockListFiles.mockResolvedValueOnce([file()]).mockResolvedValueOnce([]);
    mockDeleteFile.mockResolvedValue(undefined);

    render(<StoragePage />);
    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.actions.delete" }),
    );
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
      ).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
    );

    // On success the confirmation dialog must close.
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "list.deleteDialog.confirm" }),
      ).not.toBeInTheDocument();
    });
    expect(mockToast).toHaveBeenCalled();
  });

  it("keeps the dialog open and toasts when delete fails", async () => {
    mockListFiles.mockResolvedValue([file()]);
    mockDeleteFile.mockRejectedValue(new Error("delete failed"));

    render(<StoragePage />);
    await waitFor(() => {
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.actions.delete" }),
    );
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
      ).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      );
    });
    // The dialog stays open so the user can retry.
    expect(
      screen.getByRole("button", { name: "list.deleteDialog.confirm" }),
    ).toBeInTheDocument();
  });
});
