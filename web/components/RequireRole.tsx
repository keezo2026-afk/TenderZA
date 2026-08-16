"use client";

// Wraps an admin page so an unauthorised visitor gets a sign-in form or a
// plain explanation instead of a wall of failed fetches (§17).

import { useState } from "react";

import { ROLE_LABELS, type Role } from "@/lib/auth";
import { useAuth } from "./AuthProvider";

function SignInForm({ required }: { required: Role }) {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm rounded-lg border border-slate-200 bg-white p-5">
      <h1 className="text-lg font-semibold">Sign in</h1>
      <p className="mt-1 text-sm text-slate-500">
        This page is restricted to{" "}
        <strong>{ROLE_LABELS[required].toLowerCase()}</strong> accounts and
        above.
      </p>
      <form onSubmit={submit} className="mt-4 space-y-3">
        <div>
          <label className="text-xs font-semibold text-slate-600">Email</label>
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none"
          />
        </div>
        <div>
          <label className="text-xs font-semibold text-slate-600">
            Password
          </label>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none"
          />
        </div>
        {error && (
          <p className="rounded-md border border-red-200 bg-red-50 p-2 text-sm text-red-700">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-emerald-600 px-3 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="mt-3 text-xs text-slate-400">
        Accounts are created by an administrator with{" "}
        <code>scripts/manage_users.py</code>.
      </p>
    </div>
  );
}

export function RequireRole({
  role,
  children,
}: {
  role: Role;
  children: React.ReactNode;
}) {
  const { loading, authenticated, user, can, signOut } = useAuth();

  if (loading) {
    return <p className="text-sm text-slate-500">Checking your session…</p>;
  }

  if (!authenticated && !can(role)) {
    return <SignInForm required={role} />;
  }

  if (!can(role)) {
    return (
      <div className="mx-auto max-w-md rounded-lg border border-amber-200 bg-amber-50 p-5 text-sm">
        <h1 className="text-base font-semibold text-amber-900">
          Not authorised
        </h1>
        <p className="mt-1 text-amber-800">
          You are signed in as <strong>{user?.email}</strong> (
          {user ? ROLE_LABELS[user.role] : "unknown"}), but this page needs{" "}
          <strong>{ROLE_LABELS[role]}</strong>. Ask an administrator to change
          your role.
        </p>
        <button
          onClick={signOut}
          className="mt-3 rounded-md border border-amber-300 bg-white px-3 py-1.5 text-xs font-semibold text-amber-900 hover:bg-amber-100"
        >
          Sign out
        </button>
      </div>
    );
  }

  return <>{children}</>;
}
