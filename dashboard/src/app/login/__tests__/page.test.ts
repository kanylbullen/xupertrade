/**
 * Tests for the /login page's locked state.
 *
 * The notice used to send the operator to "a basic user in the secrets
 * manager". No such env var exists — `getAuthConfig` reads only
 * AUTH_MODE and OIDC_* from env — so an operator who followed it stayed
 * locked out, and the only thing that did work in-band was
 * AUTH_MODE=disabled, which opens the dashboard to everyone. Every path
 * the notice names now has to work from the host.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  fetchAuthConfig: vi.fn(),
}));

// The real form is a client component with router hooks; the page's
// branch choice is what's under test, not the form.
vi.mock("@/components/login-form", () => ({
  LoginForm: () => "LOGIN-FORM",
}));

import { fetchAuthConfig } from "@/lib/auth";

import LoginPage from "../page";

const mockedFetchCfg = vi.mocked(fetchAuthConfig);

async function render(params: Record<string, string> = {}): Promise<string> {
  const el = await LoginPage({ searchParams: Promise.resolve(params) });
  return renderToStaticMarkup(el);
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("LoginPage — locked", () => {
  it("names the recovery paths that work, and renders no sign-in form", async () => {
    mockedFetchCfg.mockResolvedValue({
      mode: "locked",
      basic_user_set: false,
      oidc_issuer: "",
      oidc_client_id: "",
      oidc_scopes: "openid profile email",
    });

    const html = await render({ error: "auth-locked" });

    expect(html).toContain("Authentication is locked");
    expect(html).toContain("dump.rdb");
    expect(html).toContain("AUTH_MODE=oidc");
    expect(html).toContain("OIDC_ISSUER");
    expect(html).toContain("OIDC_CLIENT_ID");
    expect(html).toContain("OIDC_CLIENT_SECRET");
    expect(html).toContain("scripts/set-basic-auth.sh");
    // The instruction that led nowhere.
    expect(html).not.toMatch(/basic\s+user\)?\s+in the secrets manager/);
    // Never recommend opening the dashboard as the way back in.
    expect(html).not.toContain("disabled");
    expect(html).not.toContain("LOGIN-FORM");
  });
});

describe("LoginPage — oidc half-configured, no basic user", () => {
  it("points at the host script instead of switching auth off", async () => {
    mockedFetchCfg.mockResolvedValue({
      mode: "oidc",
      basic_user_set: false,
      oidc_issuer: "",
      oidc_client_id: "",
      oidc_scopes: "openid profile email",
    });

    const html = await render();

    expect(html).toContain("scripts/set-basic-auth.sh");
    expect(html).not.toContain("SET dashboard:auth:mode disabled");
  });
});
