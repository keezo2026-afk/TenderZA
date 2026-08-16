import type { NextConfig } from "next";

// The browser talks only to this Next.js server; API calls are proxied
// server-side to the FastAPI backend (never call localhost from the
// browser — it is NOT the user's machine in a hosted preview).
//
// .trim() is load-bearing on Windows CMD: `set API_URL=http://... && npm run dev`
// puts everything before the `&&` into the value, trailing space included.
// That yields the destination "http://127.0.0.1:8000 /:path*", which is not a
// valid URL, and every /api/* request 500s with no obvious cause.
const API_URL = (process.env.API_URL ?? "http://127.0.0.1:8000").trim();

const nextConfig: NextConfig = {
  allowedDevOrigins: ["*.e2b.app"],
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_URL}/:path*` },
    ];
  },
};

export default nextConfig;
