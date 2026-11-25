#!/usr/bin/env python3
"""Fail-closed classifier for commands received by the Git safety hook."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Sequence

ALLOW = "allow"
REVIEW = "review"

CONTROL_OPERATORS = {";", "&&", "||", "|", "&", "\n"}
SHELL_KEYWORDS = {
    "case", "coproc", "do", "done", "elif", "else", "esac", "fi", "for",
    "function", "if", "in", "select", "then", "until", "while", "{", "}",
}
SHELLS = {"bash", "dash", "ksh", "sh", "zsh"}
UNSAFE_LAUNCHERS = {"chroot", "ionice", "nice", "runuser", "setsid", "su", "sudo", "timeout", "xargs"}
UNSAFE_ENVIRONMENT = {
    "EDITOR", "GIT_ASKPASS", "GIT_EDITOR", "GIT_PAGER", "GIT_SSH_COMMAND",
    "PAGER", "SSH_ASKPASS", "VISUAL",
}
UNSAFE_CONFIG_KEYS = {
    "core.askpass", "core.editor", "core.fsmonitor", "core.hookspath",
    "core.pager", "core.sshcommand", "credential.helper", "diff.external",
    "gpg.program", "sequence.editor",
}

# Git aliases cannot override built-ins. Unknown subcommands are blocked because
# they may resolve to a user-controlled alias or external git-* executable.
KNOWN_GIT_COMMANDS = {
    "add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch",
    "bugreport", "bundle", "cat-file", "check-attr", "check-ignore",
    "check-mailmap", "check-ref-format", "checkout", "cherry", "cherry-pick",
    "clean", "clone", "column", "commit", "commit-graph", "commit-tree",
    "config", "count-objects", "credential", "credential-cache",
    "credential-store", "describe", "diagnose", "diff", "diff-files",
    "diff-index", "diff-tree", "difftool", "fast-export", "fast-import",
    "fetch", "fetch-pack", "filter-branch", "for-each-ref", "format-patch",
    "fsck", "gc", "get-tar-commit-id", "grep", "gui", "hash-object", "help",
    "index-pack", "init", "interpret-trailers", "log", "ls-files",
    "ls-remote", "ls-tree", "mailinfo", "mailsplit", "maintenance",
    "merge", "merge-base", "merge-file", "merge-index", "merge-one-file",
    "merge-tree", "mergetool", "mktag", "mktree", "multi-pack-index", "mv",
    "name-rev", "notes", "pack-objects", "pack-redundant", "pack-refs",
    "patch-id", "prune", "prune-packed", "pull", "push", "range-diff",
    "read-tree", "rebase", "receive-pack", "reflog", "remote", "remote-ext",
    "remote-fd", "repack", "replace", "request-pull", "rerere", "reset",
    "restore", "rev-list", "rev-parse", "revert", "rm", "send-email",
    "send-pack", "shell", "shortlog", "show", "show-branch", "show-index",
    "show-ref", "sparse-checkout", "stage", "stash", "status", "stripspace",
    "submodule", "switch", "symbolic-ref", "tag", "unpack-file",
    "unpack-objects", "update-index", "update-ref", "update-server-info",
    "upload-archive", "upload-pack", "var", "verify-commit", "verify-pack",
    "verify-tag", "version", "web--browse", "whatchanged", "worktree",
    "write-tree",
}

GLOBAL_FLAGS = {
    "--bare", "--exec-path", "--glob-pathspecs", "--help", "--html-path",
    "--icase-pathspecs", "--info-path", "--literal-pathspecs", "--man-path",
    "--no-lazy-fetch", "--no-literal-pathspecs", "--no-optional-locks",
    "--no-pager", "--no-replace-objects", "--noglob-pathspecs", "--paginate",
    "--version", "-P", "-p",
}
GLOBAL_VALUE_OPTIONS = {
    "--attr-source", "--git-dir", "--namespace", "--super-prefix",
    "--work-tree", "-C", "-c",
}
WHOLE_TREE_PATHS = {".", "*", "**", ":/", ":(top)", ":(glob)**", ":(glob)**/*"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class ClassificationError(ValueError):
    """Raised when input cannot be classified safely."""


def command_from_hook_payload(payload: str) -> str:
    """Validate a PreToolUse payload and return its command string."""
    if not payload.strip():
        raise ClassificationError("hook payload is empty")
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ClassificationError("hook payload is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ClassificationError("hook payload must be a JSON object")
    tool_input = document.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ClassificationError("hook payload requires a tool_input object")
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ClassificationError("hook payload requires a non-empty command string")
    if "\x00" in command:
        raise ClassificationError("command contains a NUL byte")
    return command


def _shell_segments(command: str) -> list[list[str]]:
    if "`" in command or "$(" in command or "<(" in command or ">(" in command:
        raise ClassificationError("dynamic shell substitution cannot be classified safely")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise ClassificationError("shell command is malformed") from exc

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {"(", ")"}:
            raise ClassificationError("shell grouping cannot be classified safely")
        if token in CONTROL_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        if set(token) <= set(";&|()\n") and token:
            raise ClassificationError(f"unsupported shell operator: {token}")
        current.append(token)
    if current:
        segments.append(current)
    if not segments:
        raise ClassificationError("command contains no executable segment")
    return segments


def _strip_leading_redirections(tokens: Sequence[str], start: int) -> int:
    index = start
    while index < len(tokens):
        if tokens[index] in {"<", ">", "<<", ">>", "<<<", "<>"}:
            if index + 1 >= len(tokens):
                raise ClassificationError("redirection is missing its target")
            index += 2
            continue
        if (tokens[index].isdigit() and index + 1 < len(tokens)
                and tokens[index + 1] in {"<", ">", "<<", ">>", "<<<", "<>", ">&", "<&"}):
            if index + 2 >= len(tokens):
                raise ClassificationError("redirection is missing its target")
            index += 3
            continue
        break
    return index


def _unsafe_environment_assignment(assignment: str) -> str | None:
    variable = assignment.split("=", 1)[0].upper()
    if variable in UNSAFE_ENVIRONMENT or variable.startswith("GIT_CONFIG_"):
        return variable
    return None


def _unwrap_command(tokens: Sequence[str]) -> tuple[Sequence[str], str | None]:
    index = _strip_leading_redirections(tokens, 0)
    while index < len(tokens) and (tokens[index] == "!" or ASSIGNMENT.match(tokens[index])):
        if ASSIGNMENT.match(tokens[index]):
            variable = _unsafe_environment_assignment(tokens[index])
            if variable:
                raise ClassificationError(f"executable environment override requires review: {variable}")
        index += 1
        index = _strip_leading_redirections(tokens, index)
    if index >= len(tokens):
        raise ClassificationError("command segment has no executable")

    executable = os.path.basename(tokens[index])
    if executable in SHELL_KEYWORDS:
        raise ClassificationError(f"shell control flow cannot be classified safely: {executable}")
    if "$" in executable or "{" in executable or "}" in executable:
        raise ClassificationError("dynamic executable name cannot be classified safely")
    if executable in {"eval", "source", "."}:
        raise ClassificationError(f"dynamic shell execution cannot be classified safely: {executable}")

    if executable in {"command", "exec", "nohup", "time"}:
        index += 1
        while index < len(tokens) and tokens[index] in {"--", "-p"}:
            index += 1
        if index < len(tokens) and tokens[index].startswith("-"):
            raise ClassificationError(f"unsupported {executable} launcher option: {tokens[index]}")
        return _unwrap_command(tokens[index:])

    if executable == "env":
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if ASSIGNMENT.match(token):
                variable = _unsafe_environment_assignment(token)
                if variable:
                    raise ClassificationError(f"executable environment override requires review: {variable}")
                index += 1
                continue
            if token in {"-i", "--ignore-environment", "-0", "--null"}:
                index += 1
                continue
            if token in {"-u", "--unset", "-C", "--chdir"}:
                if index + 1 >= len(tokens):
                    raise ClassificationError("env option is missing its value")
                index += 2
                continue
            if token.startswith("--unset=") or token.startswith("--chdir="):
                index += 1
                continue
            if token in {"-S", "--split-string"} or token.startswith("--split-string="):
                raise ClassificationError("env split-string cannot be classified safely")
            if token.startswith("-"):
                raise ClassificationError(f"unsupported env option: {token}")
            break
        return _unwrap_command(tokens[index:])

    if executable in UNSAFE_LAUNCHERS:
        raise ClassificationError(f"command launcher cannot be classified safely: {executable}")

    if executable in SHELLS:
        arguments = list(tokens[index + 1:])
        command_index = None
        for offset, token in enumerate(arguments):
            if token == "-c" or (token.startswith("-") and not token.startswith("--") and "c" in token[1:]):
                command_index = offset + 1
                break
        if command_index is None or command_index >= len(arguments):
            raise ClassificationError("nested shell does not provide a literal command string")
        nested = arguments[command_index]
        if "$" in nested or "`" in nested:
            raise ClassificationError("dynamic nested shell command cannot be classified safely")
        return (), nested

    return tokens[index:], None


def _parse_config(config: str, aliases: dict[str, str]) -> str | None:
    key, separator, value = config.partition("=")
    lowered = key.lower()
    if lowered.startswith("alias."):
        name = lowered[6:]
        if not separator or not name or not value:
            return "Git alias configuration is incomplete"
        aliases[name] = value
    elif _unsafe_config_key(lowered):
        return f"executable Git configuration requires review: {key}"
    return None


def _unsafe_config_key(key: str) -> bool:
    if key in UNSAFE_CONFIG_KEYS:
        return True
    parts = key.split(".")
    return (
        (len(parts) >= 2 and parts[0] == "pager")
        or (
            len(parts) >= 3
            and (
                (parts[0] == "diff" and parts[-1] in {"command", "textconv"})
                or (parts[0] == "difftool" and parts[-1] == "cmd")
                or (parts[0] == "filter" and parts[-1] in {"clean", "process", "smudge"})
                or (parts[0] == "merge" and parts[-1] == "driver")
                or (parts[0] == "mergetool" and parts[-1] == "cmd")
                or (parts[0] == "gpg" and parts[-1] == "program")
            )
        )
    )


def _parse_git_invocation(tokens: Sequence[str]) -> tuple[str | None, list[str], dict[str, str], str | None]:
    aliases: dict[str, str] = {}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "-":
            break
        if token == "--":
            return None, [], aliases, "Git global option delimiter has no subcommand"
        if token in GLOBAL_FLAGS:
            index += 1
            continue
        if token in GLOBAL_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return None, [], aliases, f"Git global option is missing its value: {token}"
            value = tokens[index + 1]
            if token == "-c":
                reason = _parse_config(value, aliases)
                if reason:
                    return None, [], aliases, reason
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            index += 1
            continue
        if token.startswith("-c") and token != "-c":
            reason = _parse_config(token[2:], aliases)
            if reason:
                return None, [], aliases, reason
            index += 1
            continue
        if token.startswith("--config-env="):
            config = token.split("=", 1)[1]
            key, separator, _environment = config.partition("=")
            if not separator:
                return None, [], aliases, "Git config-env option is malformed"
            lowered = key.lower()
            if lowered.startswith("alias.") or _unsafe_config_key(lowered):
                return None, [], aliases, "environment-backed executable Git configuration cannot be classified safely"
            index += 1
            continue
        matched_value_option = False
        for option in GLOBAL_VALUE_OPTIONS - {"-C", "-c"} | {"--exec-path"}:
            if token.startswith(option + "="):
                matched_value_option = True
                break
        if matched_value_option:
            index += 1
            continue
        return None, [], aliases, f"unsupported Git global option: {token}"

    if index >= len(tokens):
        return None, [], aliases, None
    return tokens[index], list(tokens[index + 1:]), aliases, None


def _short_option(arguments: Sequence[str], option: str) -> bool:
    for argument in arguments:
        if argument == "--":
            break
        if argument.startswith("-") and not argument.startswith("--") and option in argument[1:]:
            return True
    return False


def _long_option(arguments: Sequence[str], *options: str) -> bool:
    accepted = set(options)
    for argument in arguments:
        if argument == "--":
            break
        name = argument.split("=", 1)[0]
        if name in accepted or (len(name) > 2 and any(option.startswith(name) for option in accepted)):
            return True
    return False


def _whole_tree_path(argument: str) -> bool:
    if argument in WHOLE_TREE_PATHS or argument.rstrip("/") == ".":
        return True
    if not argument.startswith(":(") or ")" not in argument:
        return False
    magic, path = argument[2:].split(")", 1)
    words = {word.strip().lower() for word in magic.split(",")}
    return (("top" in words or "glob" in words) and path in {"", "*", "**", "**/"})


def _classify_git(
    tokens: Sequence[str],
    alias_depth: int = 0,
    inherited_aliases: dict[str, str] | None = None,
) -> str | None:
    if any("$" in token or "`" in token or ("{" in token and "}" in token) for token in tokens):
        return "dynamic Git arguments cannot be classified safely"

    subcommand, arguments, aliases, error = _parse_git_invocation(tokens)
    aliases = {**(inherited_aliases or {}), **aliases}
    if error:
        return error
    if subcommand is None:
        return None

    normalized = subcommand.lower()
    if normalized not in KNOWN_GIT_COMMANDS:
        alias = aliases.get(normalized)
        if alias is None:
            return f"unknown Git subcommand or configured alias requires review: {subcommand}"
        if alias_depth >= 8:
            return "Git alias expansion exceeded the safe recursion limit"
        if alias.startswith("!"):
            return f"shell Git alias requires review: {subcommand}"
        try:
            expansion = shlex.split(alias, posix=True)
        except ValueError:
            return f"Git alias is malformed: {subcommand}"
        if not expansion:
            return f"Git alias is empty: {subcommand}"
        return _classify_git([tokens[0], *expansion, *arguments], alias_depth + 1, aliases)

    if normalized == "push":
        if (_short_option(arguments, "f")
                or _long_option(arguments, "--force", "--force-with-lease", "--force-if-includes")):
            return "force push can rewrite remote history"
        if any(argument.startswith("+") for argument in arguments if argument != "+"):
            return "force refspec can rewrite remote history"
        if (_short_option(arguments, "d") or _long_option(arguments, "--delete", "--mirror")
                or any(argument.startswith(":") and len(argument) > 1 for argument in arguments)):
            return "remote ref deletion can discard shared history"
    elif normalized == "reset" and _long_option(arguments, "--hard"):
        return "hard reset can discard local work"
    elif normalized == "clean" and (_short_option(arguments, "f") or _long_option(arguments, "--force")):
        return "forced clean can delete untracked files"
    elif normalized == "branch":
        deletes = _short_option(arguments, "d") or _short_option(arguments, "D") or _long_option(arguments, "--delete")
        forces = _short_option(arguments, "f") or _short_option(arguments, "D") or _long_option(arguments, "--force")
        if deletes and forces:
            return "forced branch deletion can discard unmerged work"
    elif normalized in {"checkout", "switch"}:
        if (_short_option(arguments, "f")
                or _long_option(arguments, "--force", "--discard-changes")):
            return f"forced {normalized} can discard local work"
        if normalized == "checkout" and any(_whole_tree_path(argument) for argument in arguments):
            return "whole-worktree checkout can discard changes"
        if normalized == "checkout" and _long_option(arguments, "--pathspec-from-file"):
            return "external checkout pathspec cannot be classified safely"
    elif normalized == "restore":
        if _long_option(arguments, "--pathspec-from-file"):
            return "external restore pathspec cannot be classified safely"
        if any(_whole_tree_path(argument) for argument in arguments):
            return "whole-worktree restore can discard changes"
    elif normalized == "submodule" and "foreach" in arguments:
        return "submodule shell command requires review"
    elif normalized in {"filter-branch", "rebase"}:
        return f"{normalized} rewrites commit history"
    elif normalized == "commit" and _long_option(arguments, "--amend"):
        return "amended commit rewrites commit history"
    return None


def classify_command(command: str) -> tuple[str, str | None]:
    """Return (classification, reason) for a complete shell command string."""
    try:
        segments = _shell_segments(command)
        for segment in segments:
            invocation, nested = _unwrap_command(segment)
            if nested is not None:
                classification, reason = classify_command(nested)
                if classification == REVIEW:
                    return classification, reason
                continue
            if not invocation:
                raise ClassificationError("command invocation is empty")
            executable = os.path.basename(invocation[0]).lower()
            if executable in {"git", "git.exe"}:
                git_invocation = invocation
            elif executable.startswith("git-"):
                subcommand = executable[4:].removesuffix(".exe")
                git_invocation = ["git", subcommand, *invocation[1:]]
            else:
                continue
            reason = _classify_git(git_invocation)
            if reason:
                return REVIEW, reason
        return ALLOW, None
    except ClassificationError as exc:
        return REVIEW, str(exc)


def _block(reason: str) -> int:
    print(f"BLOCKED: {reason}. The Git safety policy requires explicit review.", file=sys.stderr)
    return 2


def main(arguments: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    if argv == ["--hook-payload"]:
        try:
            command = command_from_hook_payload(sys.stdin.read())
        except ClassificationError as exc:
            return _block(str(exc))
        classification, reason = classify_command(command)
        return _block(reason or "command requires review") if classification == REVIEW else 0

    command = " ".join(argv).strip() if argv else sys.stdin.read().strip()
    classification, reason = classify_command(command)
    print(json.dumps(
        {"command": command, "classification": classification, "reason": reason},
        separators=(",", ":"),
    ))
    return 2 if classification == REVIEW else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # A classifier bug must never permit the tool call.
        print(f"BLOCKED: Git safety classifier failed closed ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(2)
