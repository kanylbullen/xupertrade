import { describe, expect, it } from "vitest";

import { botRequestErrorText } from "../bot-request-error";

describe("botRequestErrorText", () => {
  it("explains a cap of 0 — every new tenant's state since NU-8", () => {
    const body = { error: "bot-cap-exceeded", kind: "bots_over_cap", current: 0, limit: 0 };
    expect(botRequestErrorText(body, 409)).toMatch(/can't run bots yet.*operator/);
  });

  it("names the limit when a non-zero cap is reached", () => {
    const body = { error: "bot-cap-exceeded", kind: "bots_over_cap", current: 1, limit: 1 };
    expect(botRequestErrorText(body, 409)).toMatch(/at your bot limit \(1\)/);
  });

  it("passes any other server error through", () => {
    expect(botRequestErrorText({ error: "missing required secrets" }, 422)).toBe(
      "missing required secrets",
    );
  });

  it.each([null, undefined, "not json", { error: 42 }])(
    "falls back to the status for an unusable body (%s)",
    (body) => {
      expect(botRequestErrorText(body, 500)).toBe("Failed (500)");
    },
  );
});
