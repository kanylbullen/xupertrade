# CI merge gates

What CI runs on every PR, which checks the master ruleset should require,
how to turn that on, and how to merge an emergency fix past a red or
stuck check. Roadmap NU-4.

## The checks

| Check | Workflow | Runs |
|---|---|---|
| `bot` | `.github/workflows/ci.yml` | `uv sync --locked`, then `pytest -q -n auto` with the derandomised hypothesis `ci` profile |
| `dashboard` | `ci.yml` | `npm ci`, `tsc --noEmit`, lint, vitest, `next build` |
| `migrations` | `ci.yml` | `alembic upgrade head` on an empty `postgres:16-alpine`, then a check that the database is at the single head |
| `dashboard-integration` | `ci.yml` | `npm run test:integration`: the reserveBotStart row lock and the per-tenant Postgres roles against real Postgres (testcontainers) |
| `gitleaks` | `.github/workflows/secret-scan.yml` | secret scan |

CodeQL (repository default setup) also runs, but is not a required check.

`--locked` in the `bot` and `migrations` jobs, and in `bot/Dockerfile`,
fails a `uv.lock` that no longer matches `bot/pyproject.toml`. The fix is
`cd bot && uv lock`, committed with the `pyproject.toml` change.

## Making them required (operator, once `ci` is green on master)

The ruleset is `default-protection` (id 15967890), on the default branch.
In the repository settings: Rules → Rulesets → `default-protection` →
Require status checks to pass → add `bot`, `dashboard`, `migrations` and
`gitleaks`, each with the source GitHub Actions. Add
`dashboard-integration` once it has a clean record on master.

The job `name:` in the workflow is the check name. Renaming a job without
updating the ruleset leaves a required check that never reports, and
every PR then waits on it forever.

Read the result back:

```sh
gh api repos/kanylbullen/xupertrade/rulesets/15967890 \
  --jq '.rules[] | select(.type == "required_status_checks") | .parameters'
```

## Emergency bypass

On 2026-09-24 the ruleset had **no bypass actors**. Once checks are
required, nobody can merge a PR whose required checks are red or still
pending, admins included. Set the bypass up before it is needed:

- Same ruleset page → Bypass list → Add bypass → Repository admin → **For
  pull requests only**. An admin can then merge a PR past failing checks,
  and direct pushes to master stay blocked.

Using it is the operator's decision, never an agent's, and only for a
real emergency: the bot is down, or waiting on CI costs money.

1. Open the fix as a PR as usual and let CI start.
2. Comment on the PR why it cannot wait, and which check is red or pending
   and why that is accepted.
3. Merge with `gh pr merge <n> --squash --admin`, or tick the bypass box
   in the merge box.
4. Open a follow-up for whatever made the check red. The next PR is green
   again.

Do not switch the ruleset's enforcement to disabled to get a merge
through. That also drops the PR requirement and the deletion and
force-push protection for everyone, until someone remembers to turn it
back on.
