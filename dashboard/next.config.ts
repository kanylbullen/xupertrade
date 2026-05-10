import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Keep these on the Node server side instead of letting the bundler
  // (Turbopack) pull them in. dockerode → docker-modem → ssh2 ships a
  // non-ESM `crypto.js` asset that breaks the App Router build. We
  // never use the SSH-over-Docker path; running them as plain Node
  // requires sidesteps Turbopack entirely for these modules.
  serverExternalPackages: ["dockerode", "ssh2", "cpu-features"],
};

export default nextConfig;
