/**
 * Tests for the announcement-email consent API client (#2761).
 *
 * Pins the wire contract of `GET/PUT /api/v1/me/announcement-emails` and the
 * unsubscribe route: the paths, the methods, and that turning it off always
 * sends `wording_version: null` (turning off must never fail on a stale
 * version).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const mockGet = vi.fn();
const mockPut = vi.fn();
const mockPost = vi.fn();

vi.mock("./base", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    put: (...args: unknown[]) => mockPut(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

import {
  getAnnouncementEmails,
  setAnnouncementEmails,
  unsubscribeAnnouncementEmails,
} from "./announcementEmails";

const PATH = "/api/v1/me/announcement-emails";

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
});

describe("getAnnouncementEmails", () => {
  it("GETs the setting from the session route", async () => {
    const body = { offered: true, announcement_emails: false };
    mockGet.mockResolvedValue(body);

    await expect(getAnnouncementEmails()).resolves.toEqual(body);
    expect(mockGet).toHaveBeenCalledWith(PATH);
  });
});

describe("setAnnouncementEmails", () => {
  it("turning it off PUTs wording_version null even when a version is known", async () => {
    mockPut.mockResolvedValue({ recorded: true });

    await setAnnouncementEmails(false, "2026-11");

    expect(mockPut).toHaveBeenCalledTimes(1);
    expect(mockPut).toHaveBeenCalledWith(PATH, {
      announcement_emails: false,
      wording_version: null,
    });
  });

  it("turning it on PUTs the current wording version", async () => {
    mockPut.mockResolvedValue({ recorded: true });

    await setAnnouncementEmails(true, "2026-11");

    expect(mockPut).toHaveBeenCalledWith(PATH, {
      announcement_emails: true,
      wording_version: "2026-11",
    });
  });
});

describe("unsubscribeAnnouncementEmails", () => {
  it("POSTs the token to the unauthenticated unsubscribe route", async () => {
    mockPost.mockResolvedValue({ status: "accepted" });

    await expect(unsubscribeAnnouncementEmails("tok")).resolves.toEqual({
      status: "accepted",
    });
    expect(mockPost).toHaveBeenCalledWith(
      "/api/v1/announcement-emails/unsubscribe",
      { token: "tok" },
    );
  });
});
