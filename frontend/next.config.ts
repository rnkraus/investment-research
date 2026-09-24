import type { NextConfig } from "next";

const DEFAULT_API_ORIGIN = "http://127.0.0.1:8000";
const CONTAINER_API_ORIGIN = "http://api:8000";
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

function resolveApiOrigin(): string {
  const runtimeLocation =
    process.env.INVESTMENT_RESEARCH_RUNTIME_LOCATION ?? "host";
  const configuredOrigin =
    process.env.INVESTMENT_RESEARCH_API_ORIGIN ??
    (runtimeLocation === "container"
      ? CONTAINER_API_ORIGIN
      : DEFAULT_API_ORIGIN);
  const parsedOrigin = new URL(configuredOrigin);

  if (!["host", "container"].includes(runtimeLocation)) {
    throw new Error(
      "INVESTMENT_RESEARCH_RUNTIME_LOCATION must be host or container",
    );
  }

  const isAllowedOrigin =
    runtimeLocation === "container"
      ? parsedOrigin.origin === CONTAINER_API_ORIGIN
      : ["http:", "https:"].includes(parsedOrigin.protocol) &&
        LOOPBACK_HOSTS.has(parsedOrigin.hostname);

  if (
    !isAllowedOrigin ||
    parsedOrigin.username ||
    parsedOrigin.password ||
    parsedOrigin.pathname !== "/" ||
    parsedOrigin.search ||
    parsedOrigin.hash
  ) {
    throw new Error(
      "INVESTMENT_RESEARCH_API_ORIGIN is not allowed for this runtime location",
    );
  }

  return parsedOrigin.origin;
}

const apiOrigin = resolveApiOrigin();

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  experimental: {
    proxyTimeout: 120_000,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
