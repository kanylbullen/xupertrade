import { getServerStats } from "@/lib/admin/server-stats";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

export async function GET(req: Request): Promise<Response> {
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }
  const stats = await getServerStats();
  return Response.json(stats);
}
