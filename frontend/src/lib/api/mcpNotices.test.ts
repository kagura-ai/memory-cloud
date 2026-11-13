/**
 * Tests for the service-notices opt-in API client (#2762).
 *
 * Pins the wire contract of `GET/PUT /api/v1/me/mcp-notices`: the path, the
 * method, and that turning it off always sends `wording_version: null` (a
 * stale version on an off request must never be able to make "turn off" fail).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const mockGet = vi.fn();
const mockPut = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    put: (...args: unknown[]) => mockPut(...args),
  },
}));

import { getMcpNotices, setMcpNotices } from "./mcpNotices";

const PATH = "/api/v1/me/mcp-notices";

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

describe("getMcpNotices", () => {
  it("GETs the setting from the session route and returns the body", async () => {
    const body = {
      offered: true,
      mcp_notices: false,
      answered: false,
      source: null,
      recorded_at: null,
      wording_version: "2026-11",
    };
    mockGet.mockResolvedValue(body);

    await expect(getMcpNotices()).resolves.toEqual(body);
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith(PATH);
    expect(mockPut).not.toHaveBeenCalled();
  });
});

describe("setMcpNotices", () => {
  it("turning it off PUTs wording_version null even when a version is known", async () => {
    mockPut.mockResolvedValue({ recorded: true });

    await setMcpNotices(false, "2026-11");

    expect(mockPut).toHaveBeenCalledTimes(1);
    expect(mockPut).toHaveBeenCalledWith(PATH, {
      mcp_notices: false,
      wording_version: null,
    });
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("turning it off with no known version still PUTs wording_version null", async () => {
    mockPut.mockResolvedValue({ recorded: true });

    await setMcpNotices(false, null);

    expect(mockPut).toHaveBeenCalledWith(PATH, {
      mcp_notices: false,
      wording_version: null,
    });
  });

  it("turning it on PUTs the current wording version", async () => {
    const reply = {
      offered: true,
      mcp_notices: true,
      answered: true,
      source: "settings",
      recorded_at: "2026-11-13T00:00:00Z",
      wording_version: "2026-11",
      recorded: true,
    };
    mockPut.mockResolvedValue(reply);

    await expect(setMcpNotices(true, "2026-11")).resolves.toEqual(reply);
    expect(mockPut).toHaveBeenCalledTimes(1);
    expect(mockPut).toHaveBeenCalledWith(PATH, {
      mcp_notices: true,
      wording_version: "2026-11",
    });
  });

  it("propagates a refusal (e.g. 409 stale wording) to the caller", async () => {
    const err = Object.assign(new Error("stale"), { status: 409 });
    mockPut.mockRejectedValue(err);

    await expect(setMcpNotices(true, "2026-10")).rejects.toBe(err);
  });
});
