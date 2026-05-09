#!/usr/bin/env bash
# Watch a PR for Copilot's automated review and emit the data needed to
# auto-handle it (fix, push, reply, merge, deploy).
#
# Usage (in CLI):
#     ./scripts/pr-watch.sh 22
#
# Usage (from Claude Code agent):
#     Bash with run_in_background=true. Single completion notification
#     when review lands; the agent reads the output file in the next
#     turn and acts on the dumped comments.
#
# Env overrides:
#     PR_WATCH_REPO         default: owner/repo from `gh repo view`
#     PR_WATCH_INTERVAL_S   default: 30
#     PR_WATCH_TIMEOUT_MIN  default: 30
#
# Exit codes:
#     0  review ready (output dumps comments)
#     1  timeout (Copilot didn't review)
#     2  arg error / bad PR number
#
# Why this script exists: Copilot's bot user is
# `copilot-pull-request-reviewer[bot]` — the `[bot]` suffix is easy to
# forget in jq filters. Hard-coded here so the agent doesn't have to
# remember (audit-bundle-4 cycle missed a review for several minutes
# because of exactly this).

set -uo pipefail

PR="${1:?Usage: $0 <PR_NUMBER>}"
if ! [[ "$PR" =~ ^[0-9]+$ ]]; then
    echo "ERROR: PR number must be numeric, got: $PR" >&2
    exit 2
fi

REPO="${PR_WATCH_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)}"
if [[ -z "$REPO" ]]; then
    echo "ERROR: could not determine repo (set PR_WATCH_REPO or run inside a gh-aware checkout)" >&2
    exit 2
fi

INTERVAL_S="${PR_WATCH_INTERVAL_S:-30}"
TIMEOUT_MIN="${PR_WATCH_TIMEOUT_MIN:-30}"
TIMEOUT_S=$((TIMEOUT_MIN * 60))
# Two distinct GitHub identities involved:
#   - REVIEW author:  `copilot-pull-request-reviewer[bot]`
#   - COMMENT author: `Copilot`
# Both are GitHub Copilot, just exposed under different user objects
# in the API. Treat either as "Copilot-authored" for filtering.
REVIEW_BOT='copilot-pull-request-reviewer[bot]'
COMMENT_BOT='Copilot'

echo "Watching $REPO PR #$PR for Copilot review (poll ${INTERVAL_S}s, timeout ${TIMEOUT_MIN}min)"

elapsed_s=0
while [[ $elapsed_s -lt $TIMEOUT_S ]]; do
    # Note the [bot] suffix — easy to miss; this is the entire reason
    # for the script to exist.
    review_count=$(
        gh api "repos/$REPO/pulls/$PR/reviews" \
            --jq "[.[] | select(.user.login == \"$REVIEW_BOT\")] | length" \
            2>/dev/null || echo 0
    )
    if [[ "$review_count" -gt 0 ]]; then
        # Filter to Copilot-authored comments only — excludes thread
        # replies (mine, the user's, etc.) so the agent sees exactly
        # the actionable review items.
        comment_count=$(
            gh api "repos/$REPO/pulls/$PR/comments" \
                --jq "[.[] | select(.user.login == \"$COMMENT_BOT\")] | length" \
                2>/dev/null || echo 0
        )
        elapsed_min=$((elapsed_s / 60))
        echo "COPILOT_REVIEW_READY pr=$PR comments=$comment_count elapsed=${elapsed_min}min"
        echo
        echo "--- REVIEW BODY ---"
        gh api "repos/$REPO/pulls/$PR/reviews" \
            --jq ".[] | select(.user.login == \"$REVIEW_BOT\") | .body" \
            | head -50
        echo
        if [[ "$comment_count" -gt 0 ]]; then
            echo "--- INLINE COMMENTS ($comment_count Copilot-authored) ---"
            # Format: ===COMMENT id=N path=F:L === markers let the agent
            # split the dump cleanly even when bodies contain newlines.
            gh api "repos/$REPO/pulls/$PR/comments" --jq "
                .[] | select(.user.login == \"$COMMENT_BOT\") |
                \"===COMMENT id=\(.id) path=\(.path):\(.line // \"?\") ===\n\(.body)\n\"
            "
        else
            echo "--- NO INLINE COMMENTS — review is clean, proceed to merge ---"
        fi
        exit 0
    fi
    sleep "$INTERVAL_S"
    elapsed_s=$((elapsed_s + INTERVAL_S))
done

echo "TIMEOUT: no Copilot review on $REPO PR #$PR after ${TIMEOUT_MIN}min"
exit 1
