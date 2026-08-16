import type { NextConfig } from "next";

// The browser talks only to this Next.js server; API calls are proxied
// server-side to the FastAPI backend (never call localhost from the
// browser — it is NOT the user's machine in a hosted preview).
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["*.e2b.app"],
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_URL}/:path*` },
    ];
  },
};

export default nextConfig;
