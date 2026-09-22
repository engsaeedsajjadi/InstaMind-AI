/** Where API requests are proxied to. Docker compose sets the service name. */
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || "http://localhost:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },
  async rewrites() {
    // The browser only ever talks to this origin (relative /api URLs); the
    // Node server proxies to the backend — no CORS, no localhost in client
    // code, works identically behind nginx and `next dev`.
    return [
      { source: "/api/:path*", destination: `${BACKEND_ORIGIN}/api/:path*` },
    ];
  },
};

export default nextConfig;
