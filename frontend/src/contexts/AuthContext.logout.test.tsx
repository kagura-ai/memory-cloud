/**
 * What "sign out" does once a session can hold several accounts
 * (#1488 Phase 4).
 *
 * Kept apart from AuthContext.test.tsx on purpose: that file is the #678
 * silent-refresh regression guard and mocks `logout` as a zero-argument stub.
 * Widening that stub to forward a scope would put this feature's coverage
 * inside a test whose subject is something else.
 *
 * The scope reaches the server, and the ANSWER decides the navigation — a
 * surviving session must be reloaded (another account is now active), an ended
 * one must go to /login. Getting that backwards renders the departed account's
 * workspace under the new identity, which looks like data belonging to the
 * wrong user.
 */
import { render, screen, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { AuthProvider, useAuth } from "./AuthContext";
import type { LogoutScope, User } from "@/lib/auth/auth";

const mockPush = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

const mockGetCurrentUser = vi.hoisted(() => vi.fn());
const mockLogout = vi.hoisted(() => vi.fn());
vi.mock("@/lib/auth/auth", async () => ({
  ...(await vi.importActual<typeof import("@/lib/auth/auth")>(
    "@/lib/auth/auth",
  )),
  getCurrentUser: () => mockGetCurrentUser(),
  // Forwards the scope — that is the thing under test.
  logout: (scope?: LogoutScope) => mockLogout(scope),
}));

const mockClearState = vi.hoisted(() => vi.fn());
vi.mock("@/lib/auth/clearClientState", () => ({
  clearIdentityScopedClientState: () => mockClearState(),
}));

const ALICE: User = {
  id: "alice",
  email: "alice@example.com",
  name: "Alice",
  picture: "",
  role: "user",
};

function Probe() {
  const { user, logout } = useAuth();
  return (
    <div>
      <span data-testid="user">{user?.id ?? "none"}</span>
      <button data-testid="all" onClick={() => void logout("all")} />
      <button data-testid="current" onClick={() => void logout("current")} />
      <button data-testid="default" onClick={() => void logout()} />
    </div>
  );
}

let assignSpy: ReturnType<typeof vi.fn>;
// Stubbing window.location without restoring it leaks into every test file
// that runs afterwards in the same worker.
const realLocation = Object.getOwnPropertyDescriptor(window, "location");

async function renderSignedIn() {
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("user").textContent).toBe("alice"),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetCurrentUser.mockResolvedValue(ALICE);
  mockLogout.mockResolvedValue({ session_ended: true, active_user_id: null });
  assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      assign: assignSpy,
      origin: "https://app.test",
      href: "https://app.test/",
    },
  });
});

afterEach(() => {
  if (realLocation) {
    Object.defineProperty(window, "location", realLocation);
  }
  vi.restoreAllMocks();
});

describe("logout scope", () => {
  it("defaults to signing out of everything", async () => {
    // Every call site written before multi-account existed calls logout() bare.
    // If this ever defaulted to "current" those callers would quietly start
    // leaving accounts signed in.
    await renderSignedIn();
    await act(async () => screen.getByTestId("default").click());
    expect(mockLogout).toHaveBeenCalledWith("all");
  });

  it("passes the requested scope through", async () => {
    await renderSignedIn();
    await act(async () => screen.getByTestId("current").click());
    expect(mockLogout).toHaveBeenCalledWith("current");
  });
});

describe("where the user lands", () => {
  it("goes to /login when the session ended", async () => {
    await renderSignedIn();
    await act(async () => screen.getByTestId("all").click());

    expect(mockPush).toHaveBeenCalledWith("/login");
    expect(assignSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId("user").textContent).toBe("none");
  });

  it("hard-reloads when another account is still signed in", async () => {
    // Not router.push: the active workspace is a per-user column and providers
    // cache per-user data, so a client-side route change would render the
    // departed account's workspace under the remaining identity.
    mockLogout.mockResolvedValue({
      session_ended: false,
      active_user_id: "bob",
    });
    await renderSignedIn();

    await act(async () => screen.getByTestId("current").click());

    expect(assignSpy).toHaveBeenCalledWith("/");
    expect(mockPush).not.toHaveBeenCalledWith("/login");
  });

  it("does not clear the user when the session survived", async () => {
    // The reload replaces the tree anyway; blanking the user first would flash
    // the logged-out shell on the way out.
    mockLogout.mockResolvedValue({
      session_ended: false,
      active_user_id: "bob",
    });
    await renderSignedIn();

    await act(async () => screen.getByTestId("current").click());

    expect(screen.getByTestId("user").textContent).toBe("alice");
  });
});

describe("client caches", () => {
  it("are cleared on a full sign-out", async () => {
    await renderSignedIn();
    await act(async () => screen.getByTestId("all").click());
    expect(mockClearState).toHaveBeenCalled();
  });

  it("are cleared when only one account signs out", async () => {
    // The identity changed even though the session did not — this is exactly
    // the case a cookie-only sign-out would miss.
    mockLogout.mockResolvedValue({
      session_ended: false,
      active_user_id: "bob",
    });
    await renderSignedIn();
    await act(async () => screen.getByTestId("current").click());
    expect(mockClearState).toHaveBeenCalled();
  });

  it("are cleared even when the request fails", async () => {
    mockLogout.mockRejectedValue(new Error("network"));
    await renderSignedIn();
    await act(async () => screen.getByTestId("all").click());
    expect(mockClearState).toHaveBeenCalled();
  });
});

describe("when the request fails", () => {
  it("still leaves the UI signed out", async () => {
    // Never leave the app looking signed in after the user asked to leave. If
    // the session is in fact alive, /login bounces back — self-correcting,
    // unlike a stale authenticated view.
    mockLogout.mockRejectedValue(new Error("network"));
    await renderSignedIn();

    await act(async () => screen.getByTestId("current").click());

    expect(screen.getByTestId("user").textContent).toBe("none");
    expect(mockPush).toHaveBeenCalledWith("/login");
  });
});
