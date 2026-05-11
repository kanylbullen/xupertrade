import { createRedisSubscriber, CHANNEL } from "@/lib/redis";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  // Operator-only for now (Phase 6c PR ε). The bot's Event class
  // doesn't include tenant_id, so we can't filter the Redis channel
  // per-tenant — every event would leak across tenants. A follow-up
  // PR will (a) add tenant_id to the bot's Event base class and (b)
  // either filter on tenant_id here or move to per-tenant Redis
  // channels. Until then, only operator gets the live SSE stream;
  // beta tenants poll instead.
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    start(controller) {
      let subscriber: ReturnType<typeof createRedisSubscriber> | null = null;

      try {
        subscriber = createRedisSubscriber();

        subscriber.subscribe(CHANNEL, (err) => {
          if (err) {
            controller.enqueue(
              encoder.encode(`data: {"type":"error","message":"Redis subscribe failed"}\n\n`)
            );
            return;
          }
          controller.enqueue(
            encoder.encode(`data: {"type":"connected"}\n\n`)
          );
        });

        subscriber.on("message", (_channel: string, message: string) => {
          controller.enqueue(encoder.encode(`data: ${message}\n\n`));
        });

        subscriber.on("error", () => {
          controller.enqueue(
            encoder.encode(`data: {"type":"error","message":"Redis connection lost"}\n\n`)
          );
        });
      } catch {
        controller.enqueue(
          encoder.encode(`data: {"type":"error","message":"Redis unavailable"}\n\n`)
        );
      }

      // Cleanup when client disconnects
      const cleanup = () => {
        if (subscriber) {
          subscriber.unsubscribe(CHANNEL);
          subscriber.quit();
        }
      };

      // Store cleanup for cancel
      (controller as unknown as Record<string, () => void>)._cleanup = cleanup;
    },
    cancel(controller) {
      const cleanup = (controller as unknown as Record<string, () => void>)._cleanup;
      if (cleanup) cleanup();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
