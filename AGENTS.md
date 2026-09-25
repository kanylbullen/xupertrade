# AGENTS.md — fast entry point for coding agents

Terse operating guide for AI coding agents working in Kanban-managed
worktrees. **This file points; [CLAUDE.md](CLAUDE.md) is the deep manual.**
Do not restate CLAUDE.md prose here — reference it by section (`CLAUDE.md
§ N`). Copied prose drifts; the classic example is hardcoded counts
(strategies / tests / migrations) — verify from source before quoting any.
The exception is the PR rules below: this file owns them.

CLAUDE.md map: § 0 secrets · § 1 mission & definition of done · § 2 repo
layout · § 3 environment & runbook index · § 4 subagents · § 5 open backlog ·
§ 6 working principles · § 7 workflow & review · § 8 strategy evaluation ·
§ 9 pitfalls · § 10 glossary. Fixed-backlog history:
[docs/CHANGELOG.md](docs/CHANGELOG.md). Operator runbooks (deploy, merge
gates, health check, auth recovery, Postgres rotation, Phase):
[docs/runbooks/](docs/runbooks/).

## Project (one paragraph)

Autonomous crypto trading bot for [HyperLiquid](https://hyperliquid.xyz):
a Python bot (asyncio engine, registry of pluggable strategies, Postgres +
Redis) with a Next.js dashboard, Telegram control, Caddy TLS, and a
backtest CLI. Multi-tenant: the dashboard's orchestrator spawns one Docker
container per tenant per mode (paper / testnet / mainnet) — there is no
single compose stack running all three modes side-by-side (CLAUDE.md § 2).
See [README.md](README.md) for the user-facing overview and
[CLAUDE.md](CLAUDE.md) for architecture and policy.

## Worktree context (Kanban)

- Each task runs in its **own git worktree** (Kanban: `~/.cline/worktrees/<id>/`).
- Branch from the remote, not the local `master`, which can lag:
  `git fetch origin && git switch -c <type>/<name> origin/master`.
- Work **only within the task scope**. Never touch other tasks' files or the main working tree.
- The worktree is disposable: nothing is real until it is committed and pushed.

## Secrets and publication — the repo is PUBLIC

Every commit is public forever (history survives later deletion). Full
never-commit table and the leak-response drill: **CLAUDE.md § 0 — read it.**

| Never commit | Goes instead |
|---|---|
| Telegram bot token / chat ID | Phase secrets manager |
| HyperLiquid private key / API wallet key | Phase secrets manager |
| `API_KEY`, Cloudflare / OIDC / Phase tokens | Phase or Redis (see § 0 table) |
| Personal email, server IP / LAN, real hostname, wallet addresses | `$DEPLOY_HOST`-style placeholders, `you@example.com` |

**Publication rule.** It applies to everything that leaves the machine:
code, docs, plans, commit messages, PR bodies and review comments. None of
them may carry hostnames, IPs or LAN ranges, wallet or e-mail addresses,
ping/heartbeat URLs, backup or storage keys, balances, holdings or key
dates, or step-by-step details of a security gap that is still open. Those
stay in Phase, in local files on the host, or in the chat.

The `.githooks/pre-commit` secret-pattern hook blocks secret-shaped diffs
(patterns kept in sync with `.gitleaks.toml`; gitleaks itself also runs on
every PR in CI). Setup is **per clone** (git ignores `core.hooksPath` from
a checked-in config):

```bash
git config --local core.hooksPath .githooks
```

Never bypass with `--no-verify` unless verified false positive (CLAUDE.md § 0).
Even then, rephrasing the value so it no longer matches is the better fix,
and under Claude Code it is the only one: the guard hook refuses
`--no-verify` unless the operator started the session with
`XUPERTRADE_ALLOW_NO_VERIFY=1` (CLAUDE.md § 7).

## Task workflow (checklist)

1. **Investigate** — code, logs, DB (read-only). Check CLAUDE.md § 5.
2. **Branch** from `origin/master`: `feat|fix|docs|chore|refactor/<short-name>` (kebab-case).
3. **Implement** within task scope and the PR rules below.
4. **Run gates** (below) before every push.
5. **Commit** — Conventional-Commit message + `Co-Authored-By:` line (CLAUDE.md § 7).
6. **Push + open PR** — `gh pr create --base master` with the
   `## Summary` / `## Test plan` / `## Notes for reviewer` template (CLAUDE.md § 7).
   Then wait for the required CI checks (`gh pr checks <n> --watch`) and
   fix anything red before asking for review.
7. **Review before merge** — there is NO automated code reviewer. Zero
   comments on a PR means unreviewed, not approved: get a human review or
   an agent review pass and triage every finding; if no review is
   available, leave the PR open. Details: CLAUDE.md § 7.
8. **Merge is the operator's.** Do not run `gh pr merge` unless the
   operator asks for that PR.

## PR rules (every PR)

- **Size budget.** Aim for ≤ 600 added lines per PR, tests included
  (roadmap § 4.1). Put mechanical refactors (renames, moves, formatting)
  in their own PR, so a review can see the behavior change.
- **WIP limit on the order path.** At most one open PR at a time may touch
  the order path: code that decides, sizes, sends or books an order —
  `bot/hypertrade/engine/runner.py`, `engine/portfolio.py`, `exchange/`,
  `reconcile/`, and the position and trade writes in `db/repo.py`. Order-path
  PRs merge one after the other, with the gates re-run on master between
  merges. Research work that doesn't touch the order path runs in parallel.
- **Order path, money or migrations** get the full review (all eight
  `/review` angles); small diffs get 3–4.

## Guardrails (hard rules)

- **NO direct pushes to `master`.** The `default-protection` ruleset
  refuses them, and for Claude Code the PreToolUse hook
  `.claude/hooks/guard_bash.py` blocks them (and `--no-verify`) before they
  run. Emergency fixes are PRs too, which the operator merges without
  waiting for review (the required checks still apply); the hook's
  override does not get past the ruleset (CLAUDE.md § 7).
- **NO deploy to the remote server.** The operator deploys: dashboard and
  Caddy after merge, bot code in the weekly bot-deploy window
  ([docs/runbooks/deploy.md](docs/runbooks/deploy.md)).
- **Merge gates:** bot changes → `pytest` green. Dashboard changes →
  `tsc`, lint, `vitest` and `build` green. Run them locally before
  pushing. CI runs them again on every PR (`.github/workflows/ci.yml`),
  and `master` **requires** the checks `bot`, `dashboard`, `migrations`
  and `gitleaks` (`.github/workflows/secret-scan.yml`): a PR with one red
  or pending cannot merge. `dashboard-integration` and CodeQL also run
  but are not required. None of them is a code review. Details, and the
  emergency bypass, which is not set up yet:
  [docs/runbooks/merge-gates.md](docs/runbooks/merge-gates.md).
- Never bypass the pre-commit hook.

## Key commands

```bash
cd bot && uv run pytest -q                                   # bot suite (merge gate for bot changes)
cd bot && uv run python -m hypertrade.backtest --strategy <name> --days N   # backtest (saves under --tenant-id or TENANT_ID; --no-save skips)
cd dashboard && npm ci && npx tsc --noEmit && npm run lint && npm test && npm run build   # dashboard gates
python3 .claude/hooks/test_guard_bash.py                     # tests for the push/--no-verify hook
docker compose up -d                                         # local stack: postgres, redis, dashboard, caddy (no bot — see CLAUDE.md § 2)
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

Same as CLAUDE.md § 1: PR → review → merge by the operator.

- [ ] Changes committed on the task branch, pushed, PR opened.
- [ ] Gates green locally (pytest / dashboard gates, as applicable), and
      the required CI checks green on the PR.
- [ ] Within the PR rules above (size, order-path WIP limit).
- [ ] Review handled: a review pass happened (human or agent) and every
      finding is fixed or replied to. Zero comments on a PR = unreviewed,
      not done — leave it open (CLAUDE.md § 7).
- [ ] If the PR fixes a CLAUDE.md § 5 item, it moves that entry to
      `docs/CHANGELOG.md` (ticked, citing the PR number) in the same PR.

Merging and deploying are the operator's, not the task's.
