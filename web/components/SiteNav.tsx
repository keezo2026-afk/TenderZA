"use client";

// Header navigation. Admin links only appear for people who can actually use
// them; the server enforces this regardless (§17).

import Link from "next/link";

import { ROLE_LABELS } from "@/lib/auth";
import { useAuth } from "./AuthProvider";

export function SiteNav() {
  const { loading, authenticated, user, can, auth_required, signOut } =
    useAuth();

  return (
    <nav className="flex items-center gap-4 text-sm text-slate-500">
      <Link href="/alerts" className="hover:text-slate-900">
        Alerts
      </Link>
      {can("analyst") && (
        <Link href="/review" className="hover:text-slate-900">
          Review
        </Link>
      )}
      {can("admin") && (
        <Link href="/ops" className="hover:text-slate-900">
          Health
        </Link>
      )}
      <a
        href="/api/docs"
        target="_blank"
        rel="noreferrer"
        className="hover:text-slate-900"
      >
        API
      </a>

      {!auth_required && (
        <span
          title="TENDERZA_AUTH=off — every visitor has admin rights"
          className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-semibold text-amber-800"
        >
          auth off
        </span>
      )}

      {!loading && authenticated && user && (
        <span className="flex items-center gap-2 border-l border-slate-200 pl-4">
          <span className="hidden text-xs text-slate-400 sm:inline">
            {user.email} · {ROLE_LABELS[user.role]}
          </span>
          <button
            onClick={signOut}
            className="text-xs font-semibold text-slate-500 hover:text-slate-900"
          >
            Sign out
          </button>
        </span>
      )}
    </nav>
  );
}
