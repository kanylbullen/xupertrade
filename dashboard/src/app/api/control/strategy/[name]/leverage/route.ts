import { botFetch } from "@/lib/bot-api";

export const dynamic = "force-dynamic";

export async function POST(
  req: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const body = await req.json().catch(() => ({}));
  return botFetch(req, `/api/control/strategy/${encodeURIComponent(name)}/leverage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function DELETE(
  req: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  return botFetch(req, `/api/control/strategy/${encodeURIComponent(name)}/leverage/reset`, {
    method: "POST",
  });
}
