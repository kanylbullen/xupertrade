"use client";

import { useEffect, useState, useTransition } from "react";
import { useMode, withMode } from "@/lib/use-mode";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";

type State = {
  paused: boolean;
  disabled_strategies: string[];
  open_positions: number;
  equity: number;
};

export function BotControls() {
  const mode = useMode();
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  async function refresh() {
    try {
      const res = await fetch(withMode("/api/control/state", mode), { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as State;
      setState(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [mode]);

  function action(path: string) {
    startTransition(async () => {
      await fetch(withMode(path, mode), { method: "POST" }).catch(() => null);
      await refresh();
    });
  }

  if (error) {
    return (
      <Card className="border-red-500/30 bg-red-500/5">
        <CardContent className="pt-6 text-sm text-red-400">
          Bot control unavailable: {error}
        </CardContent>
      </Card>
    );
  }

  if (!state) {
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-muted-foreground">
          Loading bot status...
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Bot Controls</CardTitle>
          <Badge
            variant="outline"
            className={
              state.paused
                ? "border-yellow-500 text-yellow-400 bg-yellow-500/10"
                : "border-green-500 text-green-400 bg-green-500/10"
            }
          >
            {state.paused ? "PAUSED" : "RUNNING"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {state.paused ? (
            <Button
              size="sm"
              onClick={() => action("/api/control/resume")}
              disabled={isPending}
              className="bg-green-600 hover:bg-green-700"
            >
              ▶ Resume Trading
            </Button>
          ) : (
            <Button
              size="sm"
              onClick={() => action("/api/control/pause")}
              disabled={isPending}
              variant="outline"
              className="border-yellow-500/50 text-yellow-400 hover:bg-yellow-500/10"
            >
              ⏸ Pause Trading
            </Button>
          )}

          <AlertDialog>
            <AlertDialogTrigger
              disabled={isPending || state.open_positions === 0}
              className="inline-flex h-8 items-center justify-center rounded-md border border-red-500/50 bg-transparent px-3 text-sm font-medium text-red-400 transition-colors hover:bg-red-500/10 disabled:pointer-events-none disabled:opacity-50"
            >
              ✕ Close All Positions ({state.open_positions})
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Close all open positions?</AlertDialogTitle>
                <AlertDialogDescription>
                  This will market-close every open position immediately.
                  Realized PnL will be recorded for each. Bot will keep running
                  unless you also pause it.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  onClick={() => action("/api/control/flat-all")}
                  className="bg-red-600 hover:bg-red-700 text-white"
                >
                  Yes, close all
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>

        {state.disabled_strategies.length > 0 && (
          <div className="text-xs text-muted-foreground">
            Disabled strategies:{" "}
            {state.disabled_strategies.map((s) => (
              <Badge key={s} variant="secondary" className="mr-1 font-mono">
                {s}
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
