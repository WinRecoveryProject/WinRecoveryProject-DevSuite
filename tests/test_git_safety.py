"""Regression contract for the installed destructive-Git safety hook."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "devsuite-git" / "modes" / "safety" / "scripts"
HOOK = SCRIPTS / "block-dangerous-git.sh"
CLASSIFIER = SCRIPTS / "classify_git_command.py"

spec = importlib.util.spec_from_file_location("git_safety_classifier", CLASSIFIER)
classifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(classifier)


class GitSafetyClassifierTests(unittest.TestCase):
    def test_equivalent_destructive_forms_require_review(self):
        commands = [
            "git clean --force -d",
            "git -C repo --no-pager clean -df",
            "git branch --delete --force old-topic",
            "git branch -df old-topic",
            "git checkout -- .",
            "git checkout --pathspec-from-file=paths.txt",
            "git restore --worktree :/",
            "git restore --pathspec-from-file=paths.txt",
            "git --git-dir=.git reset HEAD --hard",
            "git push origin +main:main",
            "git push origin main --force-with-lease",
            "git push origin --delete obsolete",
            "git -c alias.nuke='reset --hard' nuke",
            "git -c alias.first=second -c alias.second='clean -f' first",
            "git -c core.pager='git clean -f' log",
            "git unknown-user-alias",
            "git status && git clean -f",
            "git status\ngit clean --for -d",
            "env LC_ALL=C git -C repo push -f origin main",
            "env GIT_PAGER='git clean -f' git log",
            "GIT_PAGER='git clean -f' git log",
            "bash -c 'git branch -D old-topic'",
            "/usr/lib/git-core/git-clean --force -d",
            "git submodule foreach 'git clean -f'",
        ]
        for command in commands:
            with self.subTest(command=command):
                decision, reason = classifier.classify_command(command)
                self.assertEqual(decision, classifier.REVIEW)
                self.assertTrue(reason)

    def test_ordinary_commands_remain_allowed(self):
        commands = [
            "git status",
            "git -C repo --no-pager diff --cached",
            "git push origin feature",
            "git branch --delete merged-topic",
            "git checkout feature",
            "git restore --staged README.md",
            "git -c color.ui=false log -1",
            "git -c core.filemode=false status",
            "git -c alias.st=status st",
            "git status && git diff",
            "echo 'git clean -f'",
        ]
        for command in commands:
            with self.subTest(command=command):
                decision, reason = classifier.classify_command(command)
                self.assertEqual(decision, classifier.ALLOW)
                self.assertIsNone(reason)

    def test_payload_validation_rejects_unsafe_state(self):
        malformed = [
            "",
            "not-json",
            "[]",
            "{}",
            '{"tool_input":null}',
            '{"tool_input":{}}',
            '{"tool_input":{"command":17}}',
            '{"tool_input":{"command":""}}',
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(classifier.ClassificationError):
                    classifier.command_from_hook_payload(payload)

    def test_dynamic_or_unparsed_commands_require_review(self):
        commands = [
            "git clean \"$FLAGS\"",
            "$(printf git) clean -f",
            "eval 'git clean -f'",
            "if git clean -f; then echo removed; fi",
            "git status 'unterminated",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classifier.classify_command(command)[0], classifier.REVIEW)


class InstalledGitSafetyHookTests(unittest.TestCase):
    @staticmethod
    def invoke(payload: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_hook_blocks_destructive_and_malformed_payloads(self):
        destructive = self.invoke({"tool_input": {"command": "git clean --force -d"}})
        self.assertEqual(destructive.returncode, 2)
        self.assertIn("BLOCKED", destructive.stderr)

        malformed = subprocess.run(
            ["bash", str(HOOK)],
            input="not-json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("BLOCKED", malformed.stderr)

    def test_hook_allows_safe_git_command(self):
        safe = self.invoke({"tool_input": {"command": "git status"}})
        self.assertEqual(safe.returncode, 0)

    def test_missing_classifier_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated_hook = Path(directory) / HOOK.name
            shutil.copyfile(HOOK, isolated_hook)
            result = subprocess.run(
                ["bash", str(isolated_hook)],
                input=json.dumps({"tool_input": {"command": "git status"}}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing or unreadable", result.stderr)


if __name__ == "__main__":
    unittest.main()
