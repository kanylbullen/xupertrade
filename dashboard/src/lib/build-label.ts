// Display form of a `/api/version` answer, the dashboard's or a bot's,
// for the build lines on /settings/bots (where /status redirects).
// Client-safe: no fs, no server-only. Both producers validate already
// (lib/build-info.ts, bot/hypertrade/version.py); anything else still
// renders as "unknown" rather than as whatever text arrived.

export type BuildInfoLike = { sha: string; built_at: string | null };

const SHA_RE = /^[0-9a-f]{7,40}$/;
const BUILT_AT_RE = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}Z$/;

/** `abc1234, built 2026-09-24 21:58 UTC`, `abc1234`, or `unknown`. */
export function formatBuild(info: BuildInfoLike | null | undefined): string {
  if (!info || typeof info.sha !== "string" || !SHA_RE.test(info.sha)) {
    return "unknown";
  }
  const short = info.sha.slice(0, 7);
  const m =
    typeof info.built_at === "string" ? BUILT_AT_RE.exec(info.built_at) : null;
  return m ? `${short}, built ${m[1]} ${m[2]} UTC` : short;
}
