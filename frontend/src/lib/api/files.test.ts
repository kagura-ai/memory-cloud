/**
 * Tests for the file-objects API client (Issue #955).
 *
 * Verifies the three frontend wrappers build the correct workspace-scoped
 * URLs against the existing backend (`backend/src/api/routes/files.py`) and
 * that the byte formatter renders human-readable sizes.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const mockGet = vi.fn();
const mockDelete = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

import { listFiles, getDownloadUrl, deleteFile, formatFileSize } from "./files";

beforeEach(() => {
  mockGet.mockReset();
  mockDelete.mockReset();
});

describe("listFiles", () => {
  it("requests files for the workspace with the default limit", async () => {
    mockGet.mockResolvedValue([]);
    await listFiles("ws-1");
    expect(mockGet).toHaveBeenCalledWith(
      "/api/v1/files?workspace_id=ws-1&limit=50",
    );
  });

  it("passes a custom limit", async () => {
    mockGet.mockResolvedValue([]);
    await listFiles("ws-1", 100);
    expect(mockGet).toHaveBeenCalledWith(
      "/api/v1/files?workspace_id=ws-1&limit=100",
    );
  });

  it("returns the file list from the API", async () => {
    const files = [{ id: "f1", filename: "a.txt" }];
    mockGet.mockResolvedValue(files);
    await expect(listFiles("ws-1")).resolves.toBe(files);
  });
});

describe("getDownloadUrl", () => {
  it("requests the presigned URL scoped to the workspace and returns it", async () => {
    mockGet.mockResolvedValue({ download_url: "https://r2/presigned" });
    const url = await getDownloadUrl("ws-1", "file-9");
    expect(mockGet).toHaveBeenCalledWith(
      "/api/v1/files/file-9/download-url?workspace_id=ws-1",
    );
    expect(url).toBe("https://r2/presigned");
  });
});

describe("deleteFile", () => {
  it("issues a workspace-scoped DELETE", async () => {
    mockDelete.mockResolvedValue({});
    await deleteFile("ws-1", "file-9");
    expect(mockDelete).toHaveBeenCalledWith(
      "/api/v1/files/file-9?workspace_id=ws-1",
    );
  });
});

describe("formatFileSize", () => {
  it("formats bytes under 1 KiB as bytes", () => {
    expect(formatFileSize(0)).toBe("0 B");
    expect(formatFileSize(512)).toBe("512 B");
  });

  it("formats kibibytes with one decimal", () => {
    expect(formatFileSize(1024)).toBe("1.0 KB");
    expect(formatFileSize(1536)).toBe("1.5 KB");
  });

  it("formats mebibytes with one decimal", () => {
    expect(formatFileSize(1024 * 1024)).toBe("1.0 MB");
  });

  it("formats gibibytes with one decimal", () => {
    expect(formatFileSize(5 * 1024 * 1024 * 1024)).toBe("5.0 GB");
  });
});
