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
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(HOOK)))
SETTINGS = os.path.join(PROJECT, ".claude", "settings.json")
OVERRIDES = ("XUPERTRADE_ALLOW_MASTER_PUSH", "XUPERTRADE_ALLOW_NO_VERIFY")


def git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


def hook_event(command: str, cwd: str, tool: str = "Bash") -> str:
    return json.dumps({
        "session_id": "test", "hook_event_name": "PreToolUse", "cwd": cwd,
        "tool_name": tool, "tool_input": {"command": command},
    })


def clean_env(**env: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    return {**base, **env}


def run_hook(command: str, cwd: str, tool: str = "Bash", **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, HOOK], input=hook_event(command, cwd, tool), text=True,
        capture_output=True, env=clean_env(**env),
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

    def assertDenied(self, command: str, cwd: str | None = None, **env: str) -> None:
        res = run_hook(command, cwd or self.repo, **env)
        self.assertEqual(res.returncode, 2, f"not blocked: {command!r}\n{res.stderr}")
        self.assertIn("Blocked by .claude/hooks/guard_bash.py", res.stderr)

    def assertAllowed(self, command: str, cwd: str | None = None, **env: str) -> None:
        res = run_hook(command, cwd or self.repo, **env)
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
            # git takes any unambiguous prefix of a long option.
            "git push --al origin",
            "git push --mirr",
            "git push --branc origin",
            "git push --rep=origin master",
            "git push --rep origin master",
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

    def test_master_push_inside_shell_control_flow(self) -> None:
        self.on("feature")
        for cmd in [
            "if ! git push origin master; then echo fail; fi",
            "for r in origin; do git push $r master; done",
            "while true; do git push origin HEAD:master; done",
            "git status && { git push origin master; }",
            "if true; then :; else git push origin master; fi",
            "bash <<'X'\ngit push origin master\nX",
            "bash -s <<X\ngit push origin master\nX",
            "cat <<'X' | sh\ngit push origin master\nX",
        ]:
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        for cmd in [
            "if git push origin feature; then echo ok; fi",
            "for f in a b; do git push origin feature; done",
            # A heredoc no shell reads is data, even when a shell runs a file.
            "cat > notes.txt <<'X'\ngit push origin master\nX\nbash ./build.sh",
        ]:
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)

    def test_tilde_and_variable_repo_paths(self) -> None:
        # The shell expands `~` and `$HOME` before git sees them; so must
        # the hook, or a push with no refspec resolves against nothing.
        home = os.path.dirname(self.repo)
        self.on("master")
        try:
            for cmd in [
                "git -C ~/repo push",
                "git -C ~/repo push origin",
                "git -C $HOME/repo push",
                'git -C "$HOME/repo" push -u origin HEAD',
                "cd ~/repo && git push",
            ]:
                with self.subTest(cmd=cmd):
                    self.assertDenied(cmd, cwd="/", HOME=home)
        finally:
            self.on("feature")
        self.assertAllowed("git -C ~/repo push", cwd="/", HOME=home)

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
            # Abbreviations git accepts for --no-verify.
            "git commit --no-veri -m x",
            "git commit --no-verif -m x",
            "git push --no-ver origin feature",
            "git commit --no-verify=1 -m x",
            # Other ways to point core.hooksPath elsewhere for one command.
            "HP=/dev/null git --config-env=core.hooksPath=HP commit -m y",
            "git --config-env core.hooksPath=HP commit -m y",
            "git -ccore.hooksPath=/dev/null commit -m y",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
            "export GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\"",
            # -n hidden behind shell control flow.
            "if git commit -n -m x; then :; fi",
            "cat <<'X' | sh\ngit commit -n -m x\nX",
        ]:
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_git_config_hooks_path(self) -> None:
        # A lasting swap disables the secret scan for every later commit.
        self.on("feature")
        for cmd in [
            "git config core.hooksPath /dev/null",
            "git config --local core.hooksPath /tmp/empty",
            "git config --global core.hooksPath /dev/null",
            "git config -f .git/config core.hooksPath /dev/null",
            "git config -f.git/config.local core.hooksPath /dev/null",
            "git config --type=path core.hookspath /dev/null",
            "git config --replace-all core.hooksPath /dev/null",
            "git config --unset core.hooksPath",
            "git config --unset-a core.hooksPath",
            "git config --local --unset-all core.hooksPath",
            "git config set core.hooksPath /dev/null",
            "git config unset core.hooksPath",
            "git config --remove-section core",
            "git config rename-section core old",
            "git config core.hooksPath /dev/null && git commit -m x",
            "git -C . config core.hooksPath /dev/null",
        ]:
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        for cmd in [
            "git config --local core.hooksPath .githooks",
            "git config core.hooksPath ./.githooks/",
            "git config set core.hooksPath .githooks",
            "git config core.hooksPath",
            "git config --get core.hooksPath",
            "git config get core.hooksPath",
            "git config --get-regexp core.hooksPath .",
            "git config --list",
            "git config -l --show-origin",
            "git config --unset push.default",
            "git config user.name x",
            "git config --remove-section alias",
        ]:
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)
        self.assertAllowed("git config --unset core.hooksPath", XUPERTRADE_ALLOW_NO_VERIFY="1")

    def test_commits_that_only_mention_the_flags(self) -> None:
        self.on("feature")
        for cmd in [
            "git commit -m 'docs: never use --no-verify'",
            "git commit -am 'fix n'",
            "git commit -sm wip",
            "git commit -F - <<'EOF'\ndocs: don't --no-verify\n\ngit push origin master is blocked\nEOF",
            "git config --local core.hooksPath .githooks",
            "grep -rn 'no-verify' .",
            # Longer options that merely start like --no-verify.
            "git merge --no-verify-signatures feature",
            "git commit --no-verbose -m x",
            "git push --atomic origin feature",
            "GIT_CONFIG_KEY_0=user.name GIT_CONFIG_VALUE_0=x git commit -m x",
        ]:
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)

    def test_unparseable_command_falls_back_to_a_coarse_scan(self) -> None:
        # A trailing comment with an apostrophe leaves shlex an open quote.
        self.on("feature")
        self.assertDenied("git push origin master  # don't")
        self.assertDenied("git commit --no-verify -m x  # it's fine")
        self.assertAllowed("git push origin feature  # don't")
        self.assertDenied("git commit --no-veri -m x  # it's fine")
        self.assertAllowed("git commit --no-verbose -m x  # it's fine")

    def test_operator_overrides(self) -> None:
        self.on("feature")
        self.assertAllowed("git push origin master", XUPERTRADE_ALLOW_MASTER_PUSH="1")
        self.assertAllowed("git commit --no-verify -m x", XUPERTRADE_ALLOW_NO_VERIFY="1")
        # Each override covers its own rule only.
        self.assertDenied("git push origin master", XUPERTRADE_ALLOW_NO_VERIFY="1")
        self.assertDenied("git commit --no-verify -m x", XUPERTRADE_ALLOW_MASTER_PUSH="1")
        self.assertDenied("git push origin master", XUPERTRADE_ALLOW_MASTER_PUSH="yes")

    def test_deny_messages_point_at_the_pr_path(self) -> None:
        # The ruleset refuses a direct push even when the guard is lifted,
        # so the master message leads with the PR path, not the override.
        self.on("feature")
        master = run_hook("git push origin master", self.repo).stderr
        self.assertIn("open one", master)
        self.assertIn("emergency", master)
        self.assertIn("ruleset refuses direct pushes", master)
        no_verify = run_hook("git commit --no-veri -m x", self.repo).stderr
        self.assertIn("git reads it as --no-verify", no_verify)
        self.assertIn("rephrase a verified false positive", no_verify)

    def test_settings_wiring_blocks_and_fails_open_without_the_script(self) -> None:
        # Run the exact command from settings.json the way Claude Code does
        # (through a shell). With the script present it blocks; with it
        # missing (a branch cut before the hook) it must let Bash through,
        # because `python3 <missing file>` exits 2, which would deny.
        with open(SETTINGS) as f:
            command = json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.on("feature")
        event = hook_event("git push origin master", self.repo)
        wired = subprocess.run(
            ["sh", "-c", command], input=event, text=True, capture_output=True,
            env=clean_env(CLAUDE_PROJECT_DIR=PROJECT),
        )
        self.assertEqual(wired.returncode, 2, wired.stderr)
        with tempfile.TemporaryDirectory() as empty:
            missing = subprocess.run(
                ["sh", "-c", command], input=event, text=True, capture_output=True,
                env=clean_env(CLAUDE_PROJECT_DIR=empty),
            )
        self.assertEqual(missing.returncode, 0, missing.stderr)

    def test_other_tools_and_bad_input_pass(self) -> None:
        res = run_hook("git push origin master", self.repo, tool="Read")
        self.assertEqual(res.returncode, 0)
        res = subprocess.run([sys.executable, HOOK], input="not json", text=True, capture_output=True)
        self.assertEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
