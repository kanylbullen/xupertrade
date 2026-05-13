/**
 * Unit tests for the `/overview/[mode]` route handler (PR A of the
 * sidebar nav refactor). Asserts param validation:
 *   - `paper` / `testnet` / `mainnet` are accepted and render the
 *     `OverviewView` with the matching mode prop.
 *   - Anything else (`foo`, empty string, capitalised etc.) calls
 *     `notFound()`.
 *
 * The view body itself is deliberately mocked — its end-to-end render
 * pulls in DB / bot-proxy / cookies, which is out of scope for a
 * route-shape unit test. The manual smoke list in the PR description
 * covers that path.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
}));

vi.mock("../../app/overview/_overview-view", () => ({
  OverviewView: vi.fn(
    (props: { mode: string }) => `OverviewView(${props.mode})`,
  ),
}));

import { notFound } from "next/navigation";
import { OverviewView } from "../../app/overview/_overview-view";
import OverviewModePage from "../../app/overview/[mode]/page";

const notFoundMock = vi.mocked(notFound);
const overviewViewMock = vi.mocked(OverviewView);

beforeEach(() => {
  notFoundMock.mockClear();
  overviewViewMock.mockClear();
});

describe("/overview/[mode] route", () => {
  it.each(["paper", "testnet", "mainnet"] as const)(
    "renders OverviewView with mode=%s",
    async (mode) => {
      const result = (await OverviewModePage({
        params: Promise.resolve({ mode }),
      })) as { type: unknown; props: { mode: string } };
      expect(notFoundMock).not.toHaveBeenCalled();
      // Server-component returns a React element node; the JSX renderer
      // hasn't been invoked yet (no ReactDOM in node env), so assert on
      // the element shape rather than spy call-count.
      expect(result.type).toBe(OverviewView);
      expect(result.props.mode).toBe(mode);
    },
  );

  it.each(["foo", "", "Paper", "main", "live"])(
    "404s on invalid mode=%s",
    async (mode) => {
      await expect(
        OverviewModePage({ params: Promise.resolve({ mode }) }),
      ).rejects.toThrow("NEXT_NOT_FOUND");
      expect(notFoundMock).toHaveBeenCalledTimes(1);
    },
  );
});
