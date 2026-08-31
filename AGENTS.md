# AGENTS.md — fast entry point for coding agents

Terse operating guide for AI coding agents working in Kanban-managed
worktrees. **This file points; [CLAUDE.md](CLAUDE.md) is the deep manual.**
Do not restate CLAUDE.md prose here — reference it by section (`CLAUDE.md
§ N`). Copied prose drifts; the classic example is hardcoded counts
(strategies / tests / migrations) — verify from source before quoting any.

CLAUDE.md map: § 0 secrets · § 1 mission & definition of done · § 2 repo
layout · § 3 environment · § 4 subagents · § 5 backlog · § 6 working
principles · § 7 workflow · § 8 strategy evaluation · § 9 pitfalls ·
§ 10 glossary.

## Project (one paragraph)

Autonomous crypto trading bot for [HyperLiquid](https://hyperliquid.xyz):
a Python bot (asyncio engine, registry of pluggable strategies, Postgres +
Redis) with a Next.js dashboard, Telegram control, Caddy TLS, and a
backtest CLI — one Docker compose stack running paper / testnet / mainnet
modes side-by-side. See [README.md](README.md) for the user-facing overview
and [CLAUDE.md](CLAUDE.md) for architecture, history, and policy.

## Worktree context (Kanban)

- Each task runs in its **own git worktree** under `.cline/worktrees/`, based on `master`.
- Work **only within the task scope**. Never touch other tasks' files or the main working tree.
- The worktree is disposable: nothing is real until it is committed and pushed.

## Secrets — the repo is PUBLIC

Every commit is public forever (history survives later deletion). Full
never-commit table and the leak-response drill: **CLAUDE.md § 0 — read it.**

| Never commit | Goes instead |
|---|---|
| Telegram bot token / chat ID | Phase secrets manager |
| HyperLiquid private key / API wallet key | Phase secrets manager |
| `API_KEY`, Cloudflare / OIDC / Phase tokens | Phase or Redis (see § 0 table) |
| Personal email, server IP / LAN, real hostname, wallet addresses | `$DEPLOY_HOST`-style placeholders, `you@example.com` |

The `.githooks/pre-commit` secret-pattern hook blocks secret-shaped diffs
(patterns kept in sync with `.gitleaks.toml`; gitleaks itself also runs on
every PR in CI). Setup is **per clone** (git ignores `core.hooksPath` from
a checked-in config):

```bash
git config --local core.hooksPath .githooks
```

Never bypass with `--no-verify` unless verified false positive (CLAUDE.md § 0).

## Task workflow (checklist)

1. **Investigate** — code, logs, DB. Check CLAUDE.md § 5 backlog; add the task if new.
2. **Branch** from master: `feat|fix|docs|chore|refactor/<short-name>` (kebab-case).
3. **Implement** within task scope.
4. **Run gates** (below) before every push.
5. **Commit** — Conventional-Commit message + `Co-Authored-By:` line (CLAUDE.md § 7).
6. **Push + open PR** — `gh pr create --fill --base master` with the
   `## Summary` / `## Test plan` / `## Notes for reviewer` template (CLAUDE.md § 7).
7. **Copilot review** — one-shot, posted only at PR open (no re-review on
   pushes). Arm `scripts/pr-watch.sh <PR>`; classify and fix or reply inline
   to every comment; never merge with unaddressed bug-flag comments.
   Details: CLAUDE.md § 7.
8. **Merge** — `gh pr merge --squash --delete-branch`.

## Guardrails (hard rules)

- **NO direct pushes to `master`.** The PR flow is the default; CLAUDE.md
  § 7's direct-to-master exceptions are the operator's call, not the agent's.
- **NO deploy to the remote server.** The operator deploys after merge.
- **Merge gates:** bot changes → `pytest` green. Dashboard changes →
  `vitest` + `build` green. There is no test/build CI — the local suite is
  the gate. Server-side checks that do run on every PR: gitleaks
  secret-scan (`.github/workflows/secret-scan.yml`) and CodeQL.
- Never bypass the pre-commit hook.

## Key commands

```bash
cd bot && uv run pytest                                      # bot suite (merge gate for bot changes)
cd bot && uv run python -m hypertrade.backtest --strategy <name> --days N   # backtest (auto-saves)
cd dashboard && npm test                                     # vitest (merge gate for dashboard changes)
cd dashboard && npm run lint && npm run build                # lint + production build
docker compose up -d                                         # local stack: postgres, redis, bots, dashboard, caddy
```

## Dashboard-specific conventions

Read [dashboard/AGENTS.md](dashboard/AGENTS.md) before writing dashboard
code — it warns that the vendored Next.js may differ from your training
data and points at the docs in `node_modules/next/dist/docs/`.

## Before debugging or naming things

- **Pitfalls:** CLAUDE.md § 9 (asyncio/asyncpg deadlock, HL `szDecimals` +
  5-significant-figure prices, `PUBLIC_URL` redirects, Telegram HTML
  escaping, …).
- **Glossary:** CLAUDE.md § 10 (mode, signal, tick, reconcile, flip-detect,
  export_state, …).

## Definition of done (Kanban scope)

- [ ] Changes committed on the task branch, pushed, PR opened.
- [ ] Gates green (pytest / vitest + build, as applicable).
- [ ] Copilot comments addressed (replied inline + fixed or dismissed).

CLAUDE.md § 5 backlog updates (move item to Done + squash-commit hash) and
deployment happen **at merge time** — not on the task branch.
