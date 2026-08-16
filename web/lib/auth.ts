// Session helpers (§17).
//
// The session token lives in an HttpOnly cookie, so this module can never see
// it — that is the point. All it does is ask the API who the browser is, and
// let components react. Because every request goes through the same-origin
// /api rewrite, the cookie rides along automatically.

export type Role = "viewer" | "analyst" | "admin";

export interface AuthUser {
  id: string;
  email: string;
  name: string | null;
  role: Role;
}

export interface AuthState {
  authenticated: boolean;
  user: AuthUser | null;
  /** False when the server is running with TENDERZA_AUTH=off (local dev). */
  auth_required: boolean;
}

const RANK: Record<Role, number> = { viewer: 0, analyst: 1, admin: 2 };

/** Mirrors the server's ladder: seniority implies the lesser roles. */
export function hasRole(user: AuthUser | null, required: Role): boolean {
  if (!user) return false;
  const held = RANK[user.role];
  return held !== undefined && held >= RANK[required];
}

export const ROLE_LABELS: Record<Role, string> = {
  viewer: "Viewer",
  analyst: "Analyst",
  admin: "Admin",
};

export async function fetchMe(): Promise<AuthState> {
  const res = await fetch("/api/auth/me", { cache: "no-store" });
  if (!res.ok) {
    // Treat an unreachable API as "not signed in" rather than crashing the
    // page — the gate below will show a sign-in prompt.
    return { authenticated: false, user: null, auth_required: true };
  }
  return res.json();
}

export async function login(
  email: string,
  password: string
): Promise<AuthUser> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail ?? "Sign-in failed");
  }
  return body.user as AuthUser;
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST" });
}
