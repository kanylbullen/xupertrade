#!/usr/bin/env python3
"""PreToolUse guard for Bash: no push to master, no hook bypass.

Claude Code runs this before every Bash tool call (wired up in
.claude/settings.json) and passes the call as JSON on stdin. Exit 0 lets
the call through; exit 2 blocks it, and Claude is shown the reason this
script prints on stderr. The settings.json command exits 0 without
starting Python when this file is missing, e.g. after switching to a
branch cut before it: Claude Code keeps the hook it read at session
start, and `python3 <missing file>` exits 2, which would block every
Bash call.

Blocked:
  * A `git push` that would update master: an explicit master refspec
    (`master`, `HEAD:master`, `+x:master`, `:master`, `refs/heads/master`,
    a wildcard destination), `--all`/`--branches`/`--mirror`, or a push
    without a refspec (or with `HEAD`) whose target resolves to master.
  * Skipping the secret-scanning pre-commit hook (CLAUDE.md § 0):
    `--no-verify` anywhere, `git commit -n`, `git -c core.hooksPath=...`,
    `git --config-env=core.hooksPath=...`, and `GIT_CONFIG_KEY_<n>` or
    `GIT_CONFIG_PARAMETERS` assignments that name core.hooksPath.
  * The same for good: `git config` that unsets core.hooksPath, sets it
    to anything but `.githooks` (the CLAUDE.md § 0 setup), or removes or
    renames the `core` section. Reading it passes.
  git accepts any unambiguous prefix of a long option, so these are
  matched too: `--no-veri`, `--al`, `--mirr`, `--rep=origin`, `--unset-a`.
  The command is found after shell reserved words (`if ! git push ...`,
  `do git push ...`, `{ git push ...; }`), assignments and wrappers
  (`sudo`, `env`, `timeout`, ...), and inside `bash -c '...'`. Heredocs
  are checked as scripts when a shell in the same Bash call reads its
  script from stdin (`bash <<EOF`, `cat <<EOF | sh`).

Overrides, for the operator. They are read from the environment Claude
Code itself was started with, so a command cannot grant one to itself:
`XUPERTRADE_ALLOW_MASTER_PUSH=1 git push ...` is still blocked.
  XUPERTRADE_ALLOW_MASTER_PUSH=1   lift this guard for master pushes. Only
                                   the local guard: the default-protection
                                   ruleset still refuses them on GitHub.
                                   An emergency fix is a PR (CLAUDE.md § 7).
  XUPERTRADE_ALLOW_NO_VERIFY=1     allow skipping git hooks

This is a guard against mistakes, not a sandbox. It does not see git
hidden behind a variable, `$(...)`, `eval` or a script file, nor
`sudo -u user git ...`, nor `git config --edit` or a config file edited
by other means.
GitHub's ruleset on master is the server-side half. Stdlib only.
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
# Shell reserved words that can stand before a command.
RESERVED = {"!", "{", "}", "if", "then", "elif", "else", "fi", "do", "done", "while", "until"}
# Words that run the command after them. Their own options are skipped,
# but an option that takes a separate value (sudo -u user) is not
# understood, so `sudo -u x git push` is not seen.
WRAPPERS = {"sudo", "env", "command", "exec", "time", "nice", "nohup", "timeout"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `GIT_CONFIG_KEY_0=core.hooksPath`, `GIT_CONFIG_PARAMETERS='core.hooksPath'=...`
HOOKS_PATH_ENV = re.compile(r"^GIT_CONFIG_(KEY_[0-9]+|PARAMETERS)=.*core\.hookspath", re.IGNORECASE)
# `<<EOF`, `<<-'EOF'`, `<< "EOF"`, but not the here-string `<<<`.
HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# --no-verify and every prefix of it down to --no-v, for unparseable text.
NO_VERIFY_TEXT = re.compile(r"(?<!\S)--no-v(?:e(?:r(?:i(?:fy?)?)?)?)?(?=[\s=]|$)")

# git global options that take the next word as their value.
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
# git push long options that push every branch, and those that take a value.
PUSH_EVERYTHING = ("--all", "--branches", "--mirror")
PUSH_VALUE_OPTS = ("--repo", "--push-option", "--receive-pack", "--exec")
# git commit short options that consume the rest of their cluster or the
# next word, so an `n` after them is part of a value, not --no-verify.
COMMIT_VALUE_SHORTS = set("mFcCtSu")
# git config: the key that points git at its hooks, and the one value
# CLAUDE.md § 0 sets it to.
HOOKS_KEY = "core.hookspath"
HOOKS_DIR = ".githooks"
# git config actions, as options (`--unset`) and as the subcommands of
# git >= 2.46 (`git config unset`). Longest first where one is a prefix
# of another, so `--unset-a` is --unset-all.
CONFIG_ACTIONS = (
    "--unset-all", "--unset", "--remove-section", "--rename-section",
    "--replace-all", "--add", "--get-all", "--get-regexp", "--get-urlmatch",
    "--get", "--list",
)
CONFIG_SUBCOMMANDS = {"get", "set", "unset", "list", "edit", "rename-section", "remove-section"}
CONFIG_UNSETS = {"--unset", "--unset-all", "unset"}
CONFIG_SECTION_OPS = {"--remove-section", "--rename-section", "remove-section", "rename-section"}
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get", "list"}
# git config options that take the next word as their value.
CONFIG_VALUE_OPTS = ("--file", "--blob", "--type", "--default", "--comment", "--value", "--url")


class Denied(Exception):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind  # "master" or "no-verify"
        self.reason = reason


def abbrev_of(arg: str, option: str, min_len: int = 3) -> bool:
    """Is `arg` (up to any `=`) `option`, or a prefix git may accept for it?

    git's option parser takes any unambiguous prefix of a long option:
    `--no-veri` is --no-verify and `--al` is --all. A prefix that is
    ambiguous makes git fail, so matching it too is harmless.
    """
    head = arg.split("=", 1)[0]
    return len(head) >= min_len and option.startswith(head)


def resolve_dir(cwd: str, path: str) -> str:
    """The directory `cd <path>` or `git -C <path>` means, as the shell would."""
    path = os.path.expandvars(os.path.expanduser(path))
    return os.path.normpath(os.path.join(cwd, path))


def strip_heredocs(text: str) -> tuple[str, list[str]]:
    """Split off heredoc bodies; drop whole-line comments; join continuations.

    A commit message in a heredoc may say "--no-verify" or "git push
    origin master" without being a command, and an apostrophe in it
    would break shlex. The bodies are returned separately, for when a
    shell reads them as its script.
    """
    text = text.replace("\\\n", " ")
    out: list[str] = []
    bodies: list[str] = []
    body: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        if pending:
            delim, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
                bodies.append("\n".join(body))
                body = []
            else:
                body.append(line)
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
        for m in HEREDOC.finditer(line):
            pending.append((m.group(3), m.group(1) == "-"))
    if pending:
        # An unterminated heredoc runs to the end of the script.
        bodies.append("\n".join(body))
    return "\n".join(out), bodies


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
        if arg.startswith("--"):
            if any(abbrev_of(arg, opt) for opt in PUSH_EVERYTHING):
                raise Denied("master", f"`git push {arg}` also pushes {PROTECTED}")
            opt = next((o for o in PUSH_VALUE_OPTS if abbrev_of(arg, o)), None)
            if opt:
                repo_given |= opt == "--repo"
                skip_next = "=" not in arg
            continue
        if arg.startswith("-"):
            skip_next = arg == "-o"
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


def check_config(args: list[str]) -> None:
    """`git config` that points core.hooksPath away from .githooks, or drops it."""
    action: str | None = None
    words: list[str] = []
    skip_next = False
    for j, arg in enumerate(args):
        if skip_next:
            skip_next = False
        elif arg == "--":
            words.extend(args[j + 1:])
            break
        elif arg.startswith("--"):
            if any(abbrev_of(arg, opt) for opt in CONFIG_VALUE_OPTS):
                skip_next = "=" not in arg
            else:
                action = next((a for a in CONFIG_ACTIONS if abbrev_of(arg, a)), action)
        elif arg.startswith("-") and arg != "-":
            # A short cluster; `-f` takes the rest of it, or the next word.
            for i, ch in enumerate(arg[1:]):
                if ch == "f":
                    skip_next = i == len(arg) - 2
                    break
                if ch == "l":
                    action = "-l"
        else:
            words.append(arg)
    if action is None and words and words[0] in CONFIG_SUBCOMMANDS:
        action = words.pop(0)
    if action in CONFIG_SECTION_OPS:
        if words and words[0].lower() == "core":
            raise Denied("no-verify", f"`git config {action} core` drops core.hooksPath")
        return
    if not words or words[0].lower() != HOOKS_KEY or action in CONFIG_READS:
        return
    if action in CONFIG_UNSETS:
        raise Denied("no-verify", f"`git config {action} {words[0]}` turns the git hooks off")
    # A set: `<key> <value>`, --add, --replace-all or `set`. The key alone reads it.
    if len(words) >= 2 and os.path.normpath(words[1]) != HOOKS_DIR:
        raise Denied("no-verify", f"`git config {words[0]} {words[1]}` swaps out the git hooks")


def check_git(args: list[str], cwd: str) -> None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in GIT_VALUE_OPTS and i + 1 < len(args):
            opt, value = arg, args[i + 1]
            i += 2
        elif arg.startswith("--"):
            # Joined form: `--config-env=<name>=<var>`, `--git-dir=<path>`.
            opt, _, value = arg.partition("=")
            i += 1
        elif arg.startswith("-"):
            opt, value = arg[:2], arg[2:]
            i += 1
        else:
            break
        if opt == "-C" and value:
            cwd = resolve_dir(cwd, value)
        elif opt in ("-c", "--config-env") and value.lower().startswith("core.hookspath"):
            raise Denied("no-verify", f"`git {opt} {value}` swaps out the git hooks")
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if sub == "commit":
        check_commit(rest)
    elif sub == "push":
        check_push(rest, cwd)
    elif sub == "config":
        check_config(rest)


def check_shell(args: list[str], cwd: str, depth: int, heredocs: list[str]) -> None:
    """`bash -c '...'` runs its argument; `bash`, `bash -s` or `| sh` run stdin."""
    for j, opt in enumerate(args):
        if not opt.startswith("-"):
            return  # a script file: not seen
        if opt.startswith("--"):
            continue
        if "c" in opt and j + 1 < len(args):
            check_script(args[j + 1], cwd, depth + 1)
            return
        if "s" in opt:
            break
    # The shell reads its script from stdin. That may be a heredoc on
    # this line or one piped in (`cat <<EOF | sh`); check every heredoc.
    for body in heredocs:
        check_script(body, cwd, depth + 1)


def check_command(argv: list[str], cwd: str, depth: int, heredocs: list[str]) -> str:
    """Check one simple command; return the cwd for the commands after it."""
    for tok in argv:
        if abbrev_of(tok, "--no-verify", min_len=len("--no-v")):
            flag = tok.split("=", 1)[0]
            alias = "" if flag == "--no-verify" else " (git reads it as --no-verify)"
            raise Denied("no-verify", f"`{flag}`{alias} skips the pre-commit secret scan")
        if HOOKS_PATH_ENV.match(tok):
            raise Denied("no-verify", f"`{tok.split('=', 1)[0]}` swaps out the git hooks")
    i = 0
    while i < len(argv):
        word = argv[i]
        if word in RESERVED or ASSIGNMENT.match(word):
            i += 1
        elif os.path.basename(word) in WRAPPERS:
            i += 1
            while i < len(argv) and (
                argv[i].startswith("-") or ASSIGNMENT.match(argv[i])
                or re.fullmatch(r"[0-9.]+[smhd]?", argv[i])
            ):
                i += 1
        else:
            break
    if i >= len(argv):
        return cwd
    prog, rest = os.path.basename(argv[i]), argv[i + 1:]
    if prog == "cd" and rest:
        target = resolve_dir(cwd, rest[0])
        return target if os.path.isdir(target) else cwd
    if prog == "git":
        check_git(rest, cwd)
    elif prog in SHELLS and depth < 3:
        check_shell(rest, cwd, depth, heredocs)
    return cwd


def check_script(text: str, cwd: str, depth: int = 0) -> None:
    text, heredocs = strip_heredocs(text)
    try:
        tokens = tokenize(text)
    except ValueError:
        # Unbalanced quotes: fall back to a coarse scan, erring on deny.
        if NO_VERIFY_TEXT.search(text):
            raise Denied("no-verify", "`--no-verify` skips the pre-commit secret scan")
        if re.search(rf"\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*[\s:+/]{PROTECTED}(?![\w./-])", text):
            raise Denied("master", f"this looks like a push to {PROTECTED}")
        return
    for argv in simple_commands(tokens):
        cwd = check_command(argv, cwd, depth, heredocs)


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
            why = ("Master only changes through a PR that the operator merges "
                   "(CLAUDE.md § 7): push a feature branch and open one. That "
                   "holds in an emergency too; the operator merges an emergency "
                   "PR without waiting for review. GitHub's default-protection "
                   f"ruleset refuses direct pushes to {PROTECTED} even when the "
                   f"operator lifts this local guard with {override}=1.")
            hint = ""
        else:
            why = ("The pre-commit hook is the first secret scan on a public "
                   "repo (CLAUDE.md § 0). Fix what it flags, or rephrase a "
                   "verified false positive so it no longer matches. Only the "
                   "operator can allow a skip, by restarting Claude Code with "
                   f"{override}=1.")
            hint = " (Searching for the flag? Leave out the leading dashes.)"
        print(
            f"Blocked by .claude/hooks/guard_bash.py: {d.reason}. {why} "
            f"A command cannot set {override} for itself.{hint}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
