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
    `${botUrl(mode)}/api/control/strategy/${encodeURIComponent(name)}/leverage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    }
  );

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    return Response.json(
      { error: `Bot API returned ${res.status}: ${text}` },
      { status: res.status }
    );
  }
  return Response.json(await res.json());
}

export async function DELETE(
  req: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const mode = parseMode(req);
  const res = await fetch(
    `${botUrl(mode)}/api/control/strategy/${encodeURIComponent(name)}/leverage/reset`,
    { method: "POST", cache: "no-store" }
  );
  if (!res.ok) {
    return Response.json({ error: `Bot API returned ${res.status}` }, { status: res.status });
  }
  return Response.json(await res.json());
}
