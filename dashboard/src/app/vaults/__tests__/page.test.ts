/**
 * Tests for the /vaults page (roadmap NU-7): it reads the tenant's
 * services-owner bot, and says so when that bot has no vault-tracking
 * address instead of silently dropping the holdings panel.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant-server", () => ({ requireTenantServer: vi.fn() }));
vi.mock("@/lib/services-owner-check", () => ({ ownerBotRow: vi.fn() }));
vi.mock("@/lib/bot-api", () => ({ getBotApiUrl: () => "http://bot:8000" }));
vi.mock("@/lib/bot-api-key", () => ({ loadBotApiKey: async () => "k" }));

import { ownerBotRow } from "@/lib/services-owner-check";
import { requireTenantServer } from "@/lib/tenant-server";

import VaultsPage from "../page";

const OPERATOR = { id: "t1", isOperator: true };

function botAnswers(address: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      Response.json(
        url.endsWith("/api/vaults/mine")
          ? { address, positions: [], total_equity_usd: 0 }
          : { vaults: [] },
      ),
    ),
  );
}

beforeEach(() => {
  delete process.env.HYPERTRADE_SERVICES_OWNER_MODE;
  vi.mocked(requireTenantServer).mockResolvedValue(OPERATOR as never);
  vi.mocked(ownerBotRow).mockResolvedValue({ id: "b1", mode: "paper" } as never);
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("VaultsPage", () => {
  it("reads the tenant's owner bot and names its mode", async () => {
    botAnswers("0x1111111111111111111111111111111111111111");
    const html = renderToStaticMarkup(await VaultsPage());
    expect(ownerBotRow).toHaveBeenCalledWith(OPERATOR);
    expect(html).toContain("Polled daily by the paper bot");
    expect(html).not.toContain("No vault-tracking address");
  });

  it("explains a missing tracking address instead of hiding the holdings", async () => {
    botAnswers("");
    const html = renderToStaticMarkup(await VaultsPage());
    expect(html).toContain("No vault-tracking address on the paper bot");
    expect(html).toContain("VAULT_TRACKING_ADDRESS");
  });
});
