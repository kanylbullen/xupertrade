/**
 * One wall-clock read per server render.
 *
 * Server components that show relative time ("3d old", "still
 * locked") need the current time, but `Date.now()` written inline in
 * a component body is an impure render: React may re-run the body and
 * get a different answer, and two sibling cards on the same page can
 * disagree about "now". `react-hooks/purity` flags exactly that.
 *
 * The fix is to read the clock once, at the top of the page's async
 * function, and pass the number down as data — every consumer then
 * renders from the same instant, and the components themselves stay
 * pure functions of their props. This helper is that single read,
 * named so the intent is visible at the call site.
 *
 * Client components must NOT use this: they need the value in state
 * (`useState(() => Date.now())` plus an interval) so the UI actually
 * advances. See `BotRuntime` in app/settings/bots/bots-client.tsx.
 */
export function requestNow(): number {
  return Date.now();
}
