"use client";

// Client-side session context (§17).
//
// This is *convenience*, not security: every gate here has a matching
// server-side check. Hiding a button the API would refuse anyway just spares
// the user a pointless 403.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  type AuthState,
  type AuthUser,
  type Role,
  fetchMe,
  hasRole,
  login as apiLogin,
  logout as apiLogout,
} from "@/lib/auth";

interface AuthContextValue extends AuthState {
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  can: (role: Role) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const ANONYMOUS: AuthState = {
  authenticated: false,
  user: null,
  auth_required: true,
};

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>(ANONYMOUS);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setState(await fetchMe());
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      const user: AuthUser = await apiLogin(email, password);
      setState({ authenticated: true, user, auth_required: true });
      await refresh();
    },
    [refresh]
  );

  const signOut = useCallback(async () => {
    await apiLogout();
    setState(ANONYMOUS);
    await refresh();
  }, [refresh]);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      loading,
      signIn,
      signOut,
      refresh,
      // When enforcement is off the server hands every caller an admin
      // principal, so the UI should not pretend otherwise.
      can: (role: Role) => !state.auth_required || hasRole(state.user, role),
    }),
    [state, loading, signIn, signOut, refresh]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
