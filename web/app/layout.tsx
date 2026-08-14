import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "TenderZA — South African Tender Search",
  description:
    "Search South African public-sector tenders aggregated from official sources. eTender OCDS data under CC BY 4.0.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-4">
            <Link href="/" className="flex items-baseline gap-2">
              <span className="text-xl font-bold tracking-tight">
                Tender<span className="text-emerald-600">ZA</span>
              </span>
              <span className="hidden text-xs text-slate-400 sm:inline">
                South African tender search
              </span>
            </Link>
            <nav className="flex items-center gap-4 text-sm text-slate-500">
              <Link href="/review" className="hover:text-slate-900">
                Review
              </Link>
              <a
                href="/api/docs"
                target="_blank"
                rel="noreferrer"
                className="hover:text-slate-900"
              >
                API
              </a>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-4 py-6">{children}</main>
        <footer className="mx-auto max-w-5xl px-4 py-8 text-xs text-slate-400">
          Summaries and links only — always verify details at the original
          source. eTender OCDS data © National Treasury, used under CC BY 4.0.
        </footer>
      </body>
    </html>
  );
}
