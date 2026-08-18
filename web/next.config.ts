import type { NextConfig } from "next";

const apiUrl = process.env.AUTOREFACTOR_API_URL || "http://127.0.0.1:8765";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${apiUrl}/:path*`,
      },
    ];
  },
};

export default nextConfig;
