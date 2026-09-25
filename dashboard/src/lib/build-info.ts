// Which build is running.
//
// `dashboard/Dockerfile` writes `build-info.json` into the runner
// stage's working directory (`/app`, which `server.js` also chdirs to)
// at image build time: the `GIT_SHA` build arg, passed through from
// the deploy environment by `docker-compose.yml`, and the UTC time the
// image was built. Outside an image (`next dev`, tests) the file is
// absent and both fields read as unknown rather than failing.
//
// Mirrors `bot/hypertrade/version.py` — same file shape, same
// validation — so `/api/version` answers the same way on both.
import "server-only";

import { readFileSync } from "node:fs";
import path from "node:path";

export type BuildInfo = { sha: string; built_at: string | null };

export const UNKNOWN_SHA = "unknown";

// A git object name, abbreviated or full. Anything else — the compose
// default "unknown", an empty arg, a stray quote that broke the JSON —
// reports as unknown instead of echoing arbitrary text on a public
// endpoint.
const SHA_RE = /^[0-9a-f]{7,40}$/;
const BUILT_AT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

export function parseBuildInfo(raw: unknown): BuildInfo {
  const obj =
    raw !== null && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as Record<string, unknown>)
      : {};
  const sha = typeof obj.sha === "string" ? obj.sha.toLowerCase() : "";
  const builtAt = obj.built_at;
  return {
    sha: SHA_RE.test(sha) ? sha : UNKNOWN_SHA,
    built_at:
      typeof builtAt === "string" && BUILT_AT_RE.test(builtAt) ? builtAt : null,
  };
}

/** Read and validate the baked-in build info; never throws. */
export function readBuildInfo(
  file: string = path.join(process.cwd(), "build-info.json"),
): BuildInfo {
  let raw: unknown = null;
  try {
    raw = JSON.parse(readFileSync(file, "utf8"));
  } catch {
    // Missing file or malformed JSON: report unknown.
  }
  return parseBuildInfo(raw);
}

let cached: BuildInfo | null = null;

/** The image's build info never changes while the process runs. */
export function getBuildInfo(): BuildInfo {
  cached ??= readBuildInfo();
  return cached;
}
