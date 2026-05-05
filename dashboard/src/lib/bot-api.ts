export type Mode = "paper" | "testnet" | "mainnet";

const URLS: Record<Mode, string> = {
  paper: process.env.BOT_API_URL_PAPER || "http://localhost:8000",
  testnet: process.env.BOT_API_URL_TESTNET || "http://localhost:8001",
  mainnet: process.env.BOT_API_URL_MAINNET || "http://localhost:8002",
};

export function botUrl(mode: Mode): string {
  return URLS[mode];
}

export const BOT_API_URL = URLS.testnet; // back-compat for older imports

function parseMode(req: Request): Mode {
  const url = new URL(req.url);
  const m = url.searchParams.get("mode");
  return m === "paper" || m === "mainnet" ? m : "testnet";
}

export async function botFetch(req: Request, path: string, init?: RequestInit) {
  const mode = parseMode(req);
  const base = botUrl(mode);

  // Forward the dashboard's API_KEY as X-Api-Key so the bot's
  // _require_auth gate accepts our control-route POSTs (pause, flat-all,
  // strategy toggle, leverage, tls/configure, auth/configure, ...).
  // Without this, those routes silently bypass auth on the bot side
  // because the dashboard never sends the header — defeating the gate.
  // Caller-supplied headers in `init.headers` take precedence so a
  // route can override (e.g. for endpoints that explicitly require a
  // different auth scheme).
  const apiKey = process.env.API_KEY || "";
  const baseHeaders: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};
  const headers = init?.headers
    ? { ...baseHeaders, ...Object.fromEntries(new Headers(init.headers)) }
    : baseHeaders;

  try {
    const res = await fetch(`${base}${path}`, {
      ...init,
      cache: "no-store",
      headers,
    });
    if (!res.ok) {
      return Response.json(
        { error: `Bot API returned ${res.status}` },
        { status: res.status === 404 ? 404 : 502 }
      );
    }
    const data = await res.json();
    return Response.json(data);
  } catch {
    return Response.json(
      { error: `Bot API at ${base} unreachable (mode=${mode})` },
      { status: 502 }
    );
  }
}
