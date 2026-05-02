import { botUrl } from "@/lib/bot-api";
import type { Mode } from "@/lib/bot-api";

export const dynamic = "force-dynamic";

function parseMode(req: Request): Mode {
  const url = new URL(req.url);
  const m = url.searchParams.get("mode");
  return m === "paper" || m === "mainnet" ? m : "testnet";
}

export async function POST(
  req: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const body = await req.json().catch(() => ({}));
  const mode = parseMode(req);

  const res = await fetch(
    `${botUrl(mode)}/api/control/strategy/${encodeURIComponent(name)}/toggle`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    }
  );

  if (!res.ok) {
    return Response.json(
      { error: `Bot API returned ${res.status}` },
      { status: 502 }
    );
  }
  return Response.json(await res.json());
}
