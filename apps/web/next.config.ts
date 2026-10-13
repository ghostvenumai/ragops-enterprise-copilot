import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // The browser talks only to this app; there are no image or font CDNs.
  images: { unoptimized: true },
  experimental: { serverActions: { bodySizeLimit: "1mb" } },
  // Bounded builds on small hosts.
  typescript: { ignoreBuildErrors: false },
  eslint: { ignoreDuringBuilds: true },
};

export default withNextIntl(nextConfig);
