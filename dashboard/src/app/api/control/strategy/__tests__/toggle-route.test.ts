/**
 * Tests for POST /api/control/strategy/[name]/toggle.
 *
 * analysis-2026-09-15 § 5, Medium: `max_active_strategies` and
 * `allowed_strategies` were dead columns — `assertCanEnableStrategy`
 * and `assertStrategyAllowed` had zero call sites while the admin UI
 * kept editing both. These pin the enforcement to the one route where
 * a tenant turns a strategy on.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

vi.mock("@/lib/bot-api", () => ({
  tenantBotFetch: vi.fn(),
}));

import { tenantBotFetch } from "@/lib/bot-api";
import { requireTenant } from "@/lib/tenant";

import { POST } from "../[name]/toggle/route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedBotFetch = vi.mocked(tenantBotFetch);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";

function makeReq(enabled?: boolean): Request {
  return new Request(
    "https://example.com/api/control/strategy/bb_short/toggle?mode=paper",
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(enabled === undefined ? {} : { enabled }),
    },
  );
}

function makeCtx(name = "bb_short") {
  return { params: Promise.resolve({ name }) };
}

function makeTenant(over: {
  maxActiveStrategies?: number | null;
  allowedStrategies?: string[] | null;
}) {
  return {
    id: TENANT_ID,
    maxActiveStrategies: over.maxActiveStrategies ?? null,
    allowedStrategies: over.allowedStrategies ?? null,
  } as Awaited<ReturnType<typeof requireTenant>>;
}

/** The bot's /api/control/config payload. `leverage` is keyed by every
 *  instantiated strategy; `disabled_strategies` is the subset off. */
function configResponse(args: {
  all: string[];
  disabled: string[];
}): Response {
  return Response.json({
    paused: false,
    disabled_strategies: args.disabled,
    leverage: Object.fromEntries(args.all.map((n) => [n, { current: 3 }])),
    allow_multi_coin: false,
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/control/strategy/[name]/toggle", () => {
  it("proxies straight through when no cap and no allowlist are set", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant({}));
    mockedBotFetch.mockResolvedValueOnce(Response.json({ ok: true }));

    const res = await POST(makeReq(true), makeCtx());
    expect(res.status).toBe(200);
    // No extra /api/control/config round trip when there's no cap.
    expect(mockedBotFetch).toHaveBeenCalledOnce();
    expect(mockedBotFetch.mock.calls[0][1]).toBe(
      "/api/control/strategy/bb_short/toggle",
    );
  });

  it("refuses to enable a strategy outside the tenant's allowlist", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ allowedStrategies: ["moon_phases"] }),
    );

    const res = await POST(makeReq(true), makeCtx("bb_short"));
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({
      error: "strategy-not-allowed",
      strategyName: "bb_short",
    });
    expect(mockedBotFetch).not.toHaveBeenCalled();
  });

  it("refuses to enable past max_active_strategies", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ maxActiveStrategies: 2 }),
    );
    // 2 of 3 enabled, cap is 2 → enabling the third is denied.
    mockedBotFetch.mockResolvedValueOnce(
      configResponse({
        all: ["moon_phases", "bb_short", "supertrend"],
        disabled: ["bb_short"],
      }),
    );

    const res = await POST(makeReq(true), makeCtx("bb_short"));
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({
      error: "strategy-cap-exceeded",
      kind: "strategies_over_cap",
      current: 2,
      limit: 2,
    });
    // Config read only — the toggle itself never reached the bot.
    expect(mockedBotFetch).toHaveBeenCalledOnce();
  });

  it("allows enabling while under the cap", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ maxActiveStrategies: 3 }),
    );
    mockedBotFetch
      .mockResolvedValueOnce(
        configResponse({
          all: ["moon_phases", "bb_short", "supertrend"],
          disabled: ["bb_short", "supertrend"],
        }),
      )
      .mockResolvedValueOnce(Response.json({ ok: true }));

    const res = await POST(makeReq(true), makeCtx("bb_short"));
    expect(res.status).toBe(200);
    expect(mockedBotFetch).toHaveBeenCalledTimes(2);
  });

  it("does not block re-enabling a strategy that is already on at the cap", async () => {
    // Sitting exactly at the cap, toggling an already-enabled strategy
    // on is a no-op. Counting it again would make the UI unusable.
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ maxActiveStrategies: 2 }),
    );
    mockedBotFetch
      .mockResolvedValueOnce(
        configResponse({
          all: ["moon_phases", "bb_short", "supertrend"],
          disabled: ["supertrend"],
        }),
      )
      .mockResolvedValueOnce(Response.json({ ok: true }));

    const res = await POST(makeReq(true), makeCtx("bb_short"));
    expect(res.status).toBe(200);
    expect(mockedBotFetch).toHaveBeenCalledTimes(2);
  });

  it("gates a body with no 'enabled' field, matching the bot's default", async () => {
    // The bot reads `body.get("enabled", True)`, so an absent field
    // enables. The gate has to default the same way or it's skippable.
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ allowedStrategies: [] }),
    );

    const res = await POST(makeReq(undefined), makeCtx("bb_short"));
    expect(res.status).toBe(409);
    expect(mockedBotFetch).not.toHaveBeenCalled();
  });

  it("never gates the disabling direction", async () => {
    // Over-cap or outside the allowlist, turning something OFF must
    // always work — that's how a tenant gets back under a cap the
    // operator lowered underneath them.
    mockedBotFetch.mockResolvedValueOnce(Response.json({ ok: true }));

    const res = await POST(makeReq(false), makeCtx("bb_short"));
    expect(res.status).toBe(200);
    expect(mockedRequireTenant).not.toHaveBeenCalled();
    expect(mockedBotFetch).toHaveBeenCalledOnce();
  });

  it("fails closed when the live strategy set can't be read", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ maxActiveStrategies: 1 }),
    );
    mockedBotFetch.mockResolvedValueOnce(Response.json({ paused: false }));

    const res = await POST(makeReq(true), makeCtx("bb_short"));
    expect(res.status).toBe(502);
    expect(await res.json()).toMatchObject({
      error: "strategy-cap-unverifiable",
    });
    expect(mockedBotFetch).toHaveBeenCalledOnce();
  });

  it("forwards the bot's error when the config read fails", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ maxActiveStrategies: 1 }),
    );
    mockedBotFetch.mockResolvedValueOnce(
      Response.json({ error: "no running paper bot for tenant" }, { status: 404 }),
    );

    const res = await POST(makeReq(true), makeCtx("bb_short"));
    expect(res.status).toBe(404);
    expect(mockedBotFetch).toHaveBeenCalledOnce();
  });
});
