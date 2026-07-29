import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Keep these on the Node server side instead of letting the bundler
  // (Turbopack) pull them in. dockerode → docker-modem → ssh2 ships a
  // non-ESM `crypto.js` asset that breaks the App Router build. We
  // never use the SSH-over-Docker path; running them as plain Node
  // requires sidesteps Turbopack entirely for these modules.
  serverExternalPackages: ["dockerode", "ssh2", "cpu-features"],
  // This app renders zero `next/image` components, so the built-in
  // image optimizer is pure attack surface: `/_next/image` is exempted
  // from the auth gate in `proxy.ts` (the matcher excludes it), which
  // makes it the one route reachable fully unauthenticated. Turning it
  // off removes the endpoint rather than leaving a dormant image
  // proxy that a future `remotePatterns` or `dangerouslyAllowSVG`
  // change would silently arm. Flip this back the moment a real
  // `next/image` usage lands.
  images: { unoptimized: true },
};

export default nextConfig;
