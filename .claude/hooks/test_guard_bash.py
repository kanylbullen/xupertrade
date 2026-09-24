#!/usr/bin/env python3
"""Tests for guard_bash.py: pipe hook JSON through it, check the verdict.

Run: python3 .claude/hooks/test_guard_bash.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard_bash.py")
OVERRIDES = ("XUPERTRADE_ALLOW_MASTER_PUSH", "XUPERTRADE_ALLOW_NO_VERIFY")


def git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


def run_hook(command: str, cwd: str, tool: str = "Bash", **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    event = {
        "session_id": "test", "hook_event_name": "PreToolUse", "cwd": cwd,
        "tool_name": tool, "tool_input": {"command": command},
    }
    return subprocess.run(
        [sys.executable, HOOK], input=json.dumps(event), text=True,
        capture_output=True, env={**base, **env},
    )


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        cls.remote = os.path.join(root, "remote.git")
        cls.repo = os.path.join(root, "repo")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", cls.remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "master", cls.repo], check=True)
        git(cls.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(cls.repo, "remote", "add", "origin", cls.remote)
        git(cls.repo, "push", "-q", "-u", "origin", "master")
        git(cls.repo, "switch", "-q", "-c", "feature")
        git(cls.repo, "push", "-q", "-u", "origin", "feature")
        # A branch cut from origin/master tracks it; with push.default=upstream
        # a bare `git push` from it would update master.
        git(cls.repo, "branch", "-q", "--track", "tracks-master", "origin/master")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def on(self, branch: str) -> None:
        git(self.repo, "switch", "-q", branch)

    def assertDenied(self, command: str, **env: str) -> None:
        res = run_hook(command, self.repo, **env)
        self.assertEqual(res.returncode, 2, f"not blocked: {command!r}\n{res.stderr}")
        self.assertIn("Blocked by .claude/hooks/guard_bash.py", res.stderr)

    def assertAllowed(self, command: str, **env: str) -> None:
        res = run_hook(command, self.repo, **env)
        self.assertEqual(res.returncode, 0, f"blocked: {command!r}\n{res.stderr}")

    def test_explicit_master_refspecs(self) -> None:
        self.on("feature")
        for cmd in [
            "git push origin master",
            "git push -u origin master",
            "git push origin HEAD:master",
            "git push origin HEAD:refs/heads/master",
            "git push origin refs/heads/master",
            "git push origin +feature:master",
            "git push --force origin master",
            "git push origin :master",
            "git push --delete origin master",
            "git push origin 'refs/heads/*:refs/heads/*'",
            "git push --repo=origin master",
            "git push --all origin",
            "git push --mirror",
        ]:
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_master_push_hidden_in_a_bigger_command(self) -> None:
        self.on("feature")
        for cmd in [
            f"cd {self.repo} && git push origin master",
            f"git -C {self.repo} push origin master",
            "git add -A\ngit push origin master",
            "git push origin master 2>&1 | tail -3",
            "git push \\\n  origin master",
            "bash -c 'git push origin master'",
            "sudo git push origin master",
            "XUPERTRADE_ALLOW_MASTER_PUSH=1 git push origin master",
        ]:
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_implicit_target_on_master(self) -> None:
        self.on("master")
        try:
            for cmd in ["git push", "git push origin", "git push -u origin HEAD", "git push origin @"]:
                with self.subTest(cmd=cmd):
                    self.assertDenied(cmd)
        finally:
            self.on("feature")

    def test_bare_push_resolving_to_master(self) -> None:
        self.on("tracks-master")
        git(self.repo, "config", "push.default", "upstream")
        try:
            self.assertDenied("git push")
        finally:
            git(self.repo, "config", "--unset", "push.default")
            self.on("feature")
        # With the default push.default=simple, git itself refuses this push
        # (the names differ), so there is nothing to block.
        self.on("tracks-master")
        try:
            self.assertAllowed("git push")
        finally:
            self.on("feature")

    def test_feature_pushes_and_read_commands_pass(self) -> None:
        self.on("feature")
        for cmd in [
            "git push -u origin feature",
            "git push origin HEAD",
            "git push",
            "git push origin docs/master-notes",
            "git push origin master:refs/heads/copy-of-master",
            "git fetch origin master",
            "git pull --ff-only origin master",
            "git log --oneline origin/master..HEAD",
            "git switch -c fix/x origin/master",
            "echo git push origin master",
            'gh pr create --base master --body "never git push origin master"',
            "# git push origin master\ngit status",
        ]:
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)

    def test_hook_bypasses(self) -> None:
        self.on("feature")
        for cmd in [
            "git commit --no-verify -m wip",
            "git commit -nm wip",
            "git commit -a -n -m wip",
            "git push --no-verify origin feature",
            "git -c core.hooksPath=/dev/null commit -m wip",
            "npm publish --no-verify",
        ]:
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_commits_that_only_mention_the_flags(self) -> None:
        self.on("feature")
        for cmd in [
            "git commit -m 'docs: never use --no-verify'",
            "git commit -am 'fix n'",
            "git commit -sm wip",
            "git commit -F - <<'EOF'\ndocs: don't --no-verify\n\ngit push origin master is blocked\nEOF",
            "git config --local core.hooksPath .githooks",
            "grep -rn 'no-verify' .",
        ]:
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)

    def test_unparseable_command_falls_back_to_a_coarse_scan(self) -> None:
        # A trailing comment with an apostrophe leaves shlex an open quote.
        self.on("feature")
        self.assertDenied("git push origin master  # don't")
        self.assertDenied("git commit --no-verify -m x  # it's fine")
        self.assertAllowed("git push origin feature  # don't")

    def test_operator_overrides(self) -> None:
        self.on("feature")
        self.assertAllowed("git push origin master", XUPERTRADE_ALLOW_MASTER_PUSH="1")
        self.assertAllowed("git commit --no-verify -m x", XUPERTRADE_ALLOW_NO_VERIFY="1")
        # Each override covers its own rule only.
        self.assertDenied("git push origin master", XUPERTRADE_ALLOW_NO_VERIFY="1")
        self.assertDenied("git commit --no-verify -m x", XUPERTRADE_ALLOW_MASTER_PUSH="1")
        self.assertDenied("git push origin master", XUPERTRADE_ALLOW_MASTER_PUSH="yes")

    def test_other_tools_and_bad_input_pass(self) -> None:
        res = run_hook("git push origin master", self.repo, tool="Read")
        self.assertEqual(res.returncode, 0)
        res = subprocess.run([sys.executable, HOOK], input="not json", text=True, capture_output=True)
        self.assertEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
