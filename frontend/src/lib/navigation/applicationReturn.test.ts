/**
 * The arrival from the application and the per-tab marker behind the plan
 * page's link back to it (#2950).
 *
 * Only `return=kagura-public`, exactly once, on the plan path counts as an
 * arrival; the marker is a timestamp in this tab's sessionStorage, valid for
 * 24 hours, kept for the page load when storage refuses the write.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resolveForwardTarget } from "@/lib/auth/resolveForwardTarget";
import { safeReturnTo } from "@/lib/auth/safeReturnTo";

import {
  APPLICATION_RETURN_LIFETIME_MS,
  APPLICATION_RETURN_LOGIN_PATH,
  APPLICATION_RETURN_STORAGE_KEY,
  PLAN_PAGE_PATH,
  forgetApplicationReturn,
  hasApplicationReturn,
  isApplicationReturnArrival,
  rememberApplicationReturn,
  resetApplicationReturnFallback,
  subscribeApplicationReturn,
} from "./applicationReturn";

const ORIGIN = "https://kmc.example.com";

beforeEach(() => {
  window.sessionStorage.clear();
  resetApplicationReturnFallback();
});

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  resetApplicationReturnFallback();
});

describe("isApplicationReturnArrival (#2950)", () => {
  it("accepts only the plan path with exactly one return=kagura-public", () => {
    expect(PLAN_PAGE_PATH).toBe("/workspace/settings/plan");
    expect(
      isApplicationReturnArrival(
        PLAN_PAGE_PATH,
        new URLSearchParams("return=kagura-public"),
      ),
    ).toBe(true);
    // Other parameters next to it change nothing; only `return` is read.
    expect(
      isApplicationReturnArrival(
        PLAN_PAGE_PATH,
        new URLSearchParams("lang=ja&return=kagura-public"),
      ),
    ).toBe(true);
    for (const path of [
      "/workspace/settings",
      "/workspace/settings/plan/",
      "/workspace/settings/general",
      "/workspace/dashboard",
      "/login",
    ]) {
      expect(
        isApplicationReturnArrival(
          path,
          new URLSearchParams("return=kagura-public"),
        ),
      ).toBe(false);
    }
    expect(
      isApplicationReturnArrival(PLAN_PAGE_PATH, new URLSearchParams()),
    ).toBe(false);
    expect(isApplicationReturnArrival(PLAN_PAGE_PATH, null)).toBe(false);
    expect(
      isApplicationReturnArrival(
        null,
        new URLSearchParams("return=kagura-public"),
      ),
    ).toBe(false);
  });

  it("ignores another value and a repeated parameter", () => {
    for (const query of [
      "return=other",
      "return=Kagura-Public",
      "return=kagura-public%20",
      "return=",
      "return",
      "return=kagura-public&return=kagura-public",
      "return=kagura-public&return=other",
      "Return=kagura-public",
      "session_id=cs_test_123&changed=1",
    ]) {
      expect(
        isApplicationReturnArrival(PLAN_PAGE_PATH, new URLSearchParams(query)),
      ).toBe(false);
    }
  });

  it("safeReturnTo and resolveForwardTarget turn the login path back into the arrival URL", () => {
    expect(APPLICATION_RETURN_LOGIN_PATH).toBe(
      "/login?return_to=%2Fworkspace%2Fsettings%2Fplan%3Freturn%3Dkagura-public",
    );
    const login = new URL(APPLICATION_RETURN_LOGIN_PATH, ORIGIN);
    expect(login.pathname).toBe("/login");
    const returnTo = safeReturnTo(login.searchParams.get("return_to"), ORIGIN);
    expect(returnTo).toBe("/workspace/settings/plan?return=kagura-public");
    const target = resolveForwardTarget(returnTo, ORIGIN);
    expect(target).toBe("/workspace/settings/plan?return=kagura-public");
    const arrival = new URL(target, ORIGIN);
    expect(
      isApplicationReturnArrival(arrival.pathname, arrival.searchParams),
    ).toBe(true);
  });
});

describe("the application-return marker (#2950)", () => {
  it("remembers, reads and forgets the marker and notifies subscribers", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeApplicationReturn(listener);
    expect(hasApplicationReturn()).toBe(false);

    vi.spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
    rememberApplicationReturn();
    expect(window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY)).toBe(
      "1800000000000",
    );
    expect(hasApplicationReturn()).toBe(true);
    expect(listener).toHaveBeenCalledTimes(1);

    // A new arrival overwrites the timestamp.
    vi.spyOn(Date, "now").mockReturnValue(1_800_000_060_000);
    rememberApplicationReturn();
    expect(window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY)).toBe(
      "1800000060000",
    );
    expect(listener).toHaveBeenCalledTimes(2);

    forgetApplicationReturn();
    expect(
      window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
    ).toBeNull();
    expect(hasApplicationReturn()).toBe(false);
    expect(listener).toHaveBeenCalledTimes(3);

    unsubscribe();
    rememberApplicationReturn();
    expect(listener).toHaveBeenCalledTimes(3);
  });

  it("ignores an old marker and a malformed value", () => {
    const now = 1_800_000_000_000;
    vi.spyOn(Date, "now").mockReturnValue(now);
    expect(APPLICATION_RETURN_LIFETIME_MS).toBe(24 * 60 * 60 * 1000);

    const valid = [
      String(now),
      String(now - APPLICATION_RETURN_LIFETIME_MS),
      String(now + APPLICATION_RETURN_LIFETIME_MS),
    ];
    for (const value of valid) {
      window.sessionStorage.setItem(APPLICATION_RETURN_STORAGE_KEY, value);
      expect(hasApplicationReturn()).toBe(true);
    }

    const ignored = [
      String(now - APPLICATION_RETURN_LIFETIME_MS - 1),
      String(now + APPLICATION_RETURN_LIFETIME_MS + 1),
      "",
      "abc",
      "-1800000000000",
      "1800000000000.5",
      " 1800000000000",
      "1e12",
      "12345678901234567",
      "https://app.example.com/return",
    ];
    for (const value of ignored) {
      window.sessionStorage.setItem(APPLICATION_RETURN_STORAGE_KEY, value);
      expect(hasApplicationReturn()).toBe(false);
      // Ignored, not deleted: the value stays until the tab ends.
      expect(
        window.sessionStorage.getItem(APPLICATION_RETURN_STORAGE_KEY),
      ).toBe(value);
    }
  });

  it("keeps the marker for this page load when storage is unavailable", () => {
    const now = 1_800_000_000_000;
    vi.spyOn(Date, "now").mockReturnValue(now);
    // On the instance: the test DOM hands out methods it looked up earlier,
    // so a spy on Storage.prototype can be bypassed.
    const blocked = () => {
      throw new Error("blocked");
    };
    const setItem = vi
      .spyOn(window.sessionStorage, "setItem")
      .mockImplementation(blocked);
    const getItem = vi
      .spyOn(window.sessionStorage, "getItem")
      .mockImplementation(blocked);
    const removeItem = vi
      .spyOn(window.sessionStorage, "removeItem")
      .mockImplementation(blocked);
    const listener = vi.fn();
    const unsubscribe = subscribeApplicationReturn(listener);

    expect(hasApplicationReturn()).toBe(false);
    expect(() => rememberApplicationReturn()).not.toThrow();
    expect(setItem).toHaveBeenCalledWith(
      APPLICATION_RETURN_STORAGE_KEY,
      String(now),
    );
    // Storage throws on every read, so this can only be the page-load marker.
    expect(hasApplicationReturn()).toBe(true);
    expect(getItem).toHaveBeenCalled();
    expect(listener).toHaveBeenCalledTimes(1);

    // The page-load marker ages like the stored one.
    vi.spyOn(Date, "now").mockReturnValue(
      now + APPLICATION_RETURN_LIFETIME_MS + 1,
    );
    expect(hasApplicationReturn()).toBe(false);
    vi.spyOn(Date, "now").mockReturnValue(now);
    expect(hasApplicationReturn()).toBe(true);

    expect(() => forgetApplicationReturn()).not.toThrow();
    expect(removeItem).toHaveBeenCalledWith(APPLICATION_RETURN_STORAGE_KEY);
    expect(hasApplicationReturn()).toBe(false);
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });
});
