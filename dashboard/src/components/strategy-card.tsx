import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type StrategyConfig = {
  id: number;
  name: string;
  symbol: string;
  timeframe: string;
  enabled: boolean | null;
  paramsJson: string | null;
};

export function StrategyCard({ strategy }: { strategy: StrategyConfig }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base font-semibold">
          {strategy.name}
        </CardTitle>
        <Badge variant={strategy.enabled ? "default" : "secondary"}>
          {strategy.enabled ? "Active" : "Disabled"}
        </Badge>
      </CardHeader>
      <CardContent>
        <div className="flex gap-4 text-sm text-muted-foreground">
          <span className="font-mono">{strategy.symbol}</span>
          <span>{strategy.timeframe}</span>
        </div>
      </CardContent>
    </Card>
  );
}
