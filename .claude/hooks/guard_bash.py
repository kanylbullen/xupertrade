#!/usr/bin/env python3
"""PreToolUse guard for Bash: no push to master, no hook bypass.

Claude Code runs this before every Bash tool call (wired up in
.claude/settings.json) and passes the call as JSON on stdin. Exit 0 lets
the call through; exit 2 blocks it, and Claude is shown the reason this
script prints on stderr.

Blocked:
  * A `git push` that would update master: an explicit master refspec
    (`master`, `HEAD:master`, `+x:master`, `:master`, `refs/heads/master`,
    a wildcard destination), `--all`/`--branches`/`--mirror`, or a push
    without a refspec (or with `HEAD`) whose target resolves to master.
  * Skipping the secret-scanning pre-commit hook (CLAUDE.md § 0):
    `--no-verify` anywhere, `git commit -n`, `git -c core.hooksPath=...`.

Overrides, for the operator in an emergency. They are read from the
environment Claude Code itself was started with, so a command cannot
grant one to itself: `XUPERTRADE_ALLOW_MASTER_PUSH=1 git push ...` is
still blocked.
  XUPERTRADE_ALLOW_MASTER_PUSH=1   allow pushes that update master
  XUPERTRADE_ALLOW_NO_VERIFY=1     allow skipping git hooks

This is a guard against mistakes, not a sandbox: a command that hides
git behind a variable or a script is not seen. GitHub's ruleset on
master is the server-side half. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = "master"
MASTER_OVERRIDE = "XUPERTRADE_ALLOW_MASTER_PUSH"
NO_VERIFY_OVERRIDE = "XUPERTRADE_ALLOW_NO_VERIFY"

SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", ")", "\n"}
SHELLS = {"bash", "sh", "zsh", "dash"}
# Words that run the command after them. Their own options are skipped,
# but an option that takes a separate value (sudo -u user) is not
# understood, so `sudo -u x git push` is not seen.
WRAPPERS = {"sudo", "env", "command", "exec", "time", "nice", "nohup", "timeout"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `<<EOF`, `<<-'EOF'`, `<< "EOF"`, but not the here-string `<<<`.
HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

# git global options that take the next word as their value.
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
# git push options that take the next word as their value.
PUSH_VALUE_OPTS = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
PUSH_EVERYTHING = {"--all", "--branches", "--mirror"}
# git commit short options that consume the rest of their cluster or the
# next word, so an `n` after them is part of a value, not --no-verify.
COMMIT_VALUE_SHORTS = set("mFcCtSu")


class Denied(Exception):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind  # "master" or "no-verify"
        self.reason = reason


def strip_heredocs(text: str) -> str:
    """Drop heredoc bodies and whole-line comments; join continuations.

    A commit message in a heredoc may say "--no-verify" or "git push
    origin master" without being a command, and an apostrophe in it
    would break shlex.
    """
    text = text.replace("\\\n", " ")
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        if pending:
            delim, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
        for m in HEREDOC.finditer(line):
            pending.append((m.group(3), m.group(1) == "-"))
    return "\n".join(out)


def tokenize(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def simple_commands(tokens: list[str]) -> list[list[str]]:
    """Split at ; & && | || ( ) and newlines; drop redirections."""
    cmds: list[list[str]] = [[]]
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
        elif tok in SEPARATORS or set(tok) <= set(";&|()\n"):
            cmds.append([])
        elif set(tok) <= set("<>&") and set(tok) & set("<>"):
            # `> file`, `2>&1`, `<< EOF`: the target is not an argument,
            # and neither is the fd number just before the operator.
            if cmds[-1] and cmds[-1][-1].isdigit():
                cmds[-1].pop()
            skip_next = True
        else:
            cmds[-1].append(tok)
    return [c for c in cmds if c]


def run_git(cwd: str, *args: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def check_commit(args: list[str]) -> None:
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            return
        if not arg.startswith("-") or arg.startswith("--") or arg == "-":
            continue
        cluster = arg[1:]
        for i, ch in enumerate(cluster):
            if ch == "n":
                raise Denied("no-verify", "`git commit -n` is --no-verify")
            if ch in COMMIT_VALUE_SHORTS:
                skip_next = i == len(cluster) - 1 and ch in "mFcCt"
                break


def check_push(args: list[str], cwd: str) -> None:
    positionals: list[str] = []
    repo_given = False
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in PUSH_EVERYTHING:
            raise Denied("master", f"`git push {arg}` also pushes {PROTECTED}")
        if arg in PUSH_VALUE_OPTS:
            repo_given |= arg == "--repo"
            skip_next = True
            continue
        if arg.startswith("-"):
            repo_given |= arg.startswith("--repo=")
            continue
        positionals.append(arg)

    refspecs = positionals if repo_given else positionals[1:]
    current = run_git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    if not refspecs:
        target = run_git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
        if current == PROTECTED or (target and target.split("/", 1)[-1] == PROTECTED):
            raise Denied("master", f"this push (no refspec) goes to {target or PROTECTED}")
        return

    for spec in refspecs:
        spec = spec.lstrip("+")
        src, sep, dst = spec.partition(":")
        if not sep:
            dst = src
        if dst in ("HEAD", "@"):
            dst = current or dst
        dst = dst.removeprefix("refs/heads/")
        if dst == PROTECTED or "*" in dst:
            raise Denied("master", f"refspec `{spec}` updates {PROTECTED}")


def check_git(args: list[str], cwd: str) -> None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in GIT_VALUE_OPTS and i + 1 < len(args):
            value = args[i + 1]
            if arg == "-C":
                cwd = os.path.join(cwd, value)
            elif arg in ("-c", "--config-env") and value.lower().startswith("core.hookspath"):
                raise Denied("no-verify", f"`git {arg} {value}` swaps out the git hooks")
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if sub == "commit":
        check_commit(rest)
    elif sub == "push":
        check_push(rest, cwd)


def check_command(argv: list[str], cwd: str, depth: int) -> str:
    """Check one simple command; return the cwd for the commands after it."""
    for tok in argv:
        if tok == "--no-verify" or tok.startswith("--no-verify="):
            raise Denied("no-verify", "`--no-verify` skips the pre-commit secret scan")
    i = 0
    while i < len(argv) and ASSIGNMENT.match(argv[i]):
        i += 1
    while i < len(argv) and os.path.basename(argv[i]) in WRAPPERS:
        i += 1
        while i < len(argv) and (
            argv[i].startswith("-") or ASSIGNMENT.match(argv[i])
            or re.fullmatch(r"[0-9.]+[smhd]?", argv[i])
        ):
            i += 1
    if i >= len(argv):
        return cwd
    prog, rest = os.path.basename(argv[i]), argv[i + 1:]
    if prog == "cd" and rest:
        target = os.path.join(cwd, os.path.expanduser(rest[0]))
        return os.path.normpath(target) if os.path.isdir(target) else cwd
    if prog == "git":
        check_git(rest, cwd)
    elif prog in SHELLS and depth < 3:
        for j, opt in enumerate(rest[:-1]):
            if opt.startswith("-") and not opt.startswith("--") and "c" in opt:
                check_script(rest[j + 1], cwd, depth + 1)
                break
    return cwd


def check_script(text: str, cwd: str, depth: int = 0) -> None:
    text = strip_heredocs(text)
    try:
        tokens = tokenize(text)
    except ValueError:
        # Unbalanced quotes: fall back to a coarse scan, erring on deny.
        if re.search(r"(?<!\S)--no-verify(?![^\s=])", text):
            raise Denied("no-verify", "`--no-verify` skips the pre-commit secret scan")
        if re.search(rf"\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*[\s:+/]{PROTECTED}(?![\w./-])", text):
            raise Denied("master", f"this looks like a push to {PROTECTED}")
        return
    for argv in simple_commands(tokens):
        cwd = check_command(argv, cwd, depth)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    cwd = event.get("cwd") or os.getcwd()
    try:
        check_script(command, cwd)
    except Denied as d:
        override = MASTER_OVERRIDE if d.kind == "master" else NO_VERIFY_OVERRIDE
        if os.environ.get(override) == "1":
            return 0
        if d.kind == "master":
            why = ("Master only changes through a reviewed PR that the operator "
                   "merges (CLAUDE.md § 7). Push a feature branch and open a PR.")
        else:
            why = ("The pre-commit hook is the first secret scan on a public "
                   "repo (CLAUDE.md § 0). Fix what it flags instead. (Searching "
                   "for the flag? Leave out the leading dashes.)")
        print(
            f"Blocked by .claude/hooks/guard_bash.py: {d.reason}. {why} "
            f"For an operator-approved emergency, the operator restarts Claude "
            f"Code with {override}=1; a command cannot set it for itself.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
