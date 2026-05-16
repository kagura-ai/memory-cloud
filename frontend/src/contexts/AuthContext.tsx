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
  type User,
} from "@/lib/auth/auth";

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  logout: () => Promise<void>;
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

  const logout = async () => {
    // Push to /login directly instead of '/' to bypass the RootPage redirect.
    // RootPage's synchronous Server Component redirect interacts badly with
    // Next.js 16 Turbopack perf instrumentation, raising a TypeError on
    // `performance.measure('​RootPage', ...)` with a negative time stamp
    // when the navigation happens via client-side router.push.
    try {
      await logoutApi();
      setUser(null);
      router.push("/login");
    } catch (error) {
      console.error("Logout failed:", error);
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
   * not eject a logged-in session. Real 401s still set `user` to null because
   * `getCurrentUser` returns null in that case (see its public-contract
   * docblock in `lib/auth/auth.ts`), which triggers the normal
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
      console.error("Failed to refetch user (silent):", error);
      // Intentionally do not setUser(null) here — see the docblock above.
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
