import { createRedisSubscriber, CHANNEL } from "@/lib/redis";

export const dynamic = "force-dynamic";

export async function GET() {
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
