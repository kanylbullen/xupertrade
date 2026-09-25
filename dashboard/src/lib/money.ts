/**
 * Money and percentage formatting for P&L figures.
 *
 * Every non-zero amount carries its sign in front of the currency
 * symbol: "+$1,234.50", "-$12.00". The overview used to build these
 * inline, two ways, and both were wrong for a loss:
 * `${v >= 0 ? "+" : ""}$${v.toFixed(2)}` gives "$-12.00", and the P&L
 * breakdown's `sign + Math.abs(v)` dropped the minus altogether, so a
 * loss read as a gain to anyone not going by colour, and in every
 * copy-paste.
 *
 * Values are rounded to cents before the sign is chosen, so -0.004
 * renders "$0.00", not "-$0.00". Non-finite input renders "—" rather
 * than "$NaN".
 */

const USD = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Round half away from zero to hundredths, so -x renders as the
 * mirror of +x (Math.round alone sends -0.005 to 0 but 0.005 to 0.01). */
function hundredths(value: number): number {
  return Math.sign(value) * Math.round(Math.abs(value) * 100);
}

/** The sign formatSignedUsd shows: -1, 0 or 1. For colouring a value
 * so that "$0.00" is never painted as a loss. */
export function moneySign(value: number): -1 | 0 | 1 {
  const cents = Number.isFinite(value) ? hundredths(value) : 0;
  return cents > 0 ? 1 : cents < 0 ? -1 : 0;
}

/** "+$1.23", "-$1.23" or "$0.00" — for P&L, fees, funding, changes. */
export function formatSignedUsd(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const cents = hundredths(value);
  if (cents === 0) return "$0.00";
  return `${cents < 0 ? "-" : "+"}$${USD.format(Math.abs(cents) / 100)}`;
}

/** "$1.23" or "-$1.23" — for balances such as equity, where a plus
 * sign would read as a change. */
export function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const cents = hundredths(value);
  return `${cents < 0 ? "-" : ""}$${USD.format(Math.abs(cents) / 100)}`;
}

/** "+1.23%", "-1.23%" or "0.00%". */
export function formatSignedPct(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const h = hundredths(value);
  if (h === 0) return "0.00%";
  return `${h < 0 ? "-" : "+"}${(Math.abs(h) / 100).toFixed(2)}%`;
}
