/**
 * Identity-scoped client state is dropped on identity change (#1488 Phase 4).
 *
 * The interesting assertions here are the NEGATIVE ones. Clearing too much is
 * as wrong as clearing too little: wipe `theme` or `kagura_locale` and the one
 * human who keeps a work account and a personal account has the app forget how
 * they like it every time they move between them.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { clearIdentityScopedClientState } from "./clearClientState";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("clearIdentityScopedClientState", () => {
  it("drops the workspace preselect", () => {
    // The visible symptom: sign out as A, back in as B, and B's workspace
    // picker opens on A's workspace.
    localStorage.setItem("kagura_last_workspace_id", "ws-belonging-to-alice");
    clearIdentityScopedClientState();
    expect(localStorage.getItem("kagura_last_workspace_id")).toBeNull();
  });

  it("drops per-user onboarding progress", () => {
    localStorage.setItem("onboarding:dismissed", "true");
    clearIdentityScopedClientState();
    expect(localStorage.getItem("onboarding:dismissed")).toBeNull();
  });

  it("drops EVERY feature-guide key, not every other one", () => {
    // removeItem() re-indexes localStorage, so deleting inside a forward
    // key(i) walk skips entries. With three keys a naive loop leaves one
    // behind; this fails on that implementation and passes on the
    // collect-then-remove one.
    localStorage.setItem("feature-guide:contexts", "open");
    localStorage.setItem("feature-guide:memories", "closed");
    localStorage.setItem("feature-guide:storage", "open");

    clearIdentityScopedClientState();

    expect(
      Object.keys(localStorage).filter((k) => k.startsWith("feature-guide:")),
    ).toEqual([]);
  });

  it("KEEPS device preferences", () => {
    // Not identity: the person set these on this machine, and none of them
    // says anything about who is signed in.
    localStorage.setItem("theme", "dark");
    localStorage.setItem("kagura_locale", "ja");
    localStorage.setItem("sidebar-collapsed-sections", '{"admin":true}');

    clearIdentityScopedClientState();

    expect(localStorage.getItem("theme")).toBe("dark");
    expect(localStorage.getItem("kagura_locale")).toBe("ja");
    expect(localStorage.getItem("sidebar-collapsed-sections")).toBe(
      '{"admin":true}',
    );
  });

  it("does not throw when storage is unavailable", () => {
    // Private mode / disabled storage / quota. The server has already ended
    // the session by the time this runs, so failing to tidy must not surface
    // as a failed sign-out.
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    localStorage.setItem("kagura_last_workspace_id", "ws-1");

    expect(() => clearIdentityScopedClientState()).not.toThrow();
  });
});
