"use client";

/**
 * Authentication Context
 *
 * Provides user authentication state and methods throughout the application.
 */

import {
  createContext,
  useContext,
  useEffect,
  useState,
  ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import {
  getCurrentUser,
  logout as logoutApi,
  type LogoutScope,
  type User,
} from "@/lib/auth/auth";
import { clearIdentityScopedClientState } from "@/lib/auth/clearClientState";

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  /** Defaults to "all" — see the implementation for the two outcomes. */
  logout: (scope?: LogoutScope) => Promise<void>;
  refetchUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();

  // Mock auth for development (controlled by environment variable)
  const useMockAuth =
    process.env.NODE_ENV === "development" &&
    process.env.NEXT_PUBLIC_ENABLE_MOCK_AUTH === "true";

  const fetchUser = async () => {
    try {
      setIsLoading(true);

      // Use mock user if enabled
      if (useMockAuth) {
        console.warn("⚠️ MOCK AUTH ENABLED - Development only!");
        setUser({
          id: "mock-dev-user-001",
          email: "dev@localhost",
          name: "Developer (Mock)",
          picture: "",
          role: "admin",
        });
        setIsLoading(false);
        return;
      }

      const currentUser = await getCurrentUser();
      setUser(currentUser);
    } catch (error) {
      console.error("Failed to fetch user:", error);
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchUser();
  }, []);

  /**
   * Sign out — of the active account, or of every account (#1488 Phase 4).
   *
   * Defaults to "all", so the callers that predate multi-account behave
   * exactly as they did.
   *
   * Two outcomes, and they need different navigations:
   *
   * - the session ENDED → there is no identity left, so go to /login;
   * - the session SURVIVED (another account is now active) → a hard reload,
   *   for the same reason switching accounts does one (see
   *   `useAccountSwitcher.switchTo`): the active workspace is a per-user
   *   column and several providers cache per-user data, so a client-side
   *   route change would leave the departed account's workspace and contexts
   *   on screen under the new identity.
   *
   * Push to /login directly instead of '/' to bypass the RootPage redirect.
   * RootPage's synchronous Server Component redirect interacts badly with
   * Next.js 16 Turbopack perf instrumentation, raising a TypeError on
   * `performance.measure('​RootPage', ...)` with a negative time stamp
   * when the navigation happens via client-side router.push.
   */
  const logout = async (scope: LogoutScope = "all") => {
    try {
      const result = await logoutApi(scope);
      // Both outcomes change who the cached data belongs to.
      clearIdentityScopedClientState();

      if (!result.session_ended) {
        window.location.assign("/");
        return;
      }
      setUser(null);
      router.push("/login");
    } catch (error) {
      console.error("Logout failed:", error);
      // Conservative on failure: never leave the UI looking signed in when the
      // user asked to leave. If the session is in fact still alive, /login
      // bounces back — self-correcting, unlike a stale authenticated view.
      clearIdentityScopedClientState();
      setUser(null);
      router.push("/login");
    }
  };

  /**
   * In-session silent refresh.
   *
   * MUST NOT flip `isLoading` — toggling it would unmount the authenticated
   * subtree via DashboardContent's spinner gate (layout.tsx), destroying the
   * state of inner Contexts (WorkspaceProvider, MemoryContextProvider)
   * mid-flow. See issue #678 for the tree-killer pattern.
   *
   * Transient refetch errors do NOT log the user out — a network blip should
   * not eject a logged-in session. Definitive auth failures (401) DO clear the
   * session. Two paths reach that outcome:
   *   - `getCurrentUser` returns null for a 401 under its current public
   *     contract (see its docblock in `lib/auth/auth.ts`); the success path
   *     then `setUser(null)`-s.
   *   - If a 401 ever surfaces as a thrown error instead, the catch below
   *     branches on `status === 401` and clears the user directly. This makes
   *     the 401-vs-transient distinction explicit at the AuthContext layer and
   *     removes the implicit dependency on `getCurrentUser`'s null-for-401
   *     contract (issue #683).
   * Either way the user lands on null, triggering the normal
   * redirect-to-login path through DashboardContent.
   *
   * In dev mock-auth mode (`useMockAuth`), refetch is a no-op — the mock
   * identity is static and the real `getCurrentUser` would 401 against the
   * dev backend, clearing the mock user.
   */
  const refetchUser = async () => {
    if (useMockAuth) {
      return;
    }
    try {
      const currentUser = await getCurrentUser();
      setUser(currentUser);
    } catch (error) {
      const status = (error as { status?: number } | null | undefined)?.status;
      if (status === 401) {
        // Definitive auth failure — fall through the normal logout path.
        setUser(null);
      } else {
        // Transient (5xx, network, etc.) — preserve current user state.
        console.error("Failed to refetch user (silent, transient):", error);
      }
    }
  };

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: user !== null,
    logout,
    refetchUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
