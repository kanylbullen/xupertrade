# Review Team — repo-native reviewer prompts

This directory holds **repo-native prompts for the `review-team` skill**
(`~/.agents/skills/review-team`). The skill launches a fleet of independent
reviewer subagents (correctness, security, architecture, conventions,
simplicity, UX, reliability, telemetry, testing, compatibility,
documentation) against a change. When it runs inside this repository it
resolves reviewer prompts from `.github/review-team/reviewers/` **first**,
falling back to the skill's own generic templates only for lenses this
directory does not define.

The filenames match the skill's global set (same 11 names) — keep them in
sync so the skill's core/specialist list keeps working.

## Why this directory exists

A docs-task review ran before this directory existed. The skill found no
repo-local prompts and fell back to templates discovered in a **stale
Kanban worktree from a different project**
(`~/.cline/worktrees/e0464/tagaro/.github/review-team/reviewers/`). Those
prompts were written for Tagaro/ScreenTimeAgent — Supabase RLS,
guardian/child roles, `src/lib/people.ts`, a `purchase_shop_item` RPC —
none of which exist here. The reviewer had to translate every mandate on
the fly, and a lens that assumes the wrong stack can only produce generic
findings.

**Rule going forward: prompts in this directory must be repo-native.** No
copied content from other projects — no guardian/child, no Supabase/RLS
assumptions from elsewhere, no molnkontakt. Every hook points at real
hypertrade files, commands, and CLAUDE.md sections.

## Conventions for these files

- Structure follows the skill's global templates (mandate → Review Focus →
  **Check especially (hypertrade)** → Stay In Your Lane → Review Method →
  Output Format) so the skill's fleet wrapper and aggregation keep working
  unchanged.
- **Reference, don't duplicate**: hooks cite `CLAUDE.md § N` and concrete
  file paths instead of restating doctrine. Copied prose drifts — the same
  rule the root AGENTS.md applies to itself.
- **No hardcoded counts** (strategies / tests / migrations): verify from
  source; counts are the classic drift example (AGENTS.md).

## The lenses

| File | Hypertrade emphasis |
|---|---|
| `correctness-reviewer.md` | Pine ports 1:1 vs `tv-source/`, None-sentinel SL, `export_state`/`restore_from_json` round-trips, DB↔exchange lockstep |
| `security-abuse-reviewer.md` | PUBLIC-repo secrets policy, `X-Api-Key` on bot endpoints, passwordless loopback Redis, tenant RLS + per-tenant PG roles, Argon2id/AES-GCM secret crypto |
| `architecture-reviewer.md` | DB-before-order + reconcile safety net, per-tenant dockerode orchestrator, mode isolation (paper/testnet/mainnet) |
| `code-quality-conventions-reviewer.md` | Conventional Commits + PR template (§ 7), logs-are-the-API contract, Telegram discipline |
| `performance-reliability-reviewer.md` | tenacity on reads, O(n²) strategy math vs backtest runtime, tick-loop budget, asyncpg pitfalls |
| `testing-strategy-reviewer.md` | pytest / vitest+build merge gates, the strategy-test shape, differential evidence for refactors |
| `documentation-dx-reviewer.md` | CLAUDE.md source of truth, AGENTS.md non-duplication rule, PUBLIC_URL/redirect pitfalls |
| `api-compatibility-reviewer.md` | bot API ↔ `bot-api.ts` contract, Alembic + saved-state compat, Redis key namespaces |
| `telemetry-observability-reviewer.md` | logs contract, event-bus/Telegram noise bounds, heartbeat + audit-log coverage |
| `product-ux-accessibility-reviewer.md` | mode/tenant clarity in the dashboard, password-manager inputs, auth fallback UX |
| `simplicity-scope-reviewer.md` | backlog-is-the-roadmap, no second source of truth, § 7 ask-first scope |

## Updating

When behavior changes, update CLAUDE.md first (it is the source of truth);
touch these prompts only where a lens should start looking somewhere new.
Keep the structure stable and every hook verifiable against the codebase.
