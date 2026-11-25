"""Security regressions for private, link-safe SDD artifacts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "devsuite-implement" / "modes" / "subagent" / "scripts"
WORKSPACE = SCRIPTS / "sdd-workspace"
TASK_BRIEF = SCRIPTS / "task-brief"
REVIEW_PACKAGE = SCRIPTS / "review-package"
ARTIFACT = SCRIPTS / "sdd-artifact"


class SddArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="devsuite-sdd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.host_temp = self.root / "host-temp"
        self.host_temp.mkdir()
        self.environment = os.environ.copy()
        self.environment.update({
            "TMPDIR": str(self.host_temp),
            "TMP": str(self.host_temp),
            "TEMP": str(self.host_temp),
        })
        self.git("init", "-q")
        self.git("config", "user.name", "DevSuite Tests")
        self.git("config", "user.email", "devsuite@example.invalid")
        self.plan = self.repo / "plan.md"
        self.plan.write_text(
            "# Plan\n\n## Task 1\nFirst requirement.\n\n## Task 2\nSecond requirement.\n",
            encoding="utf-8",
        )

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=self.repo, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def run_script(self, script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *(str(value) for value in arguments)],
            cwd=self.repo,
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def workspace(self) -> Path:
        result = self.run_script(WORKSPACE, self.plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip())

    def commit_range(self) -> tuple[str, str]:
        self.git("add", "plan.md")
        self.git("commit", "-q", "-m", "base")
        base = self.git("rev-parse", "HEAD")
        (self.repo / "change.txt").write_text("reviewed change\n", encoding="utf-8")
        self.git("add", "change.txt")
        self.git("commit", "-q", "-m", "change")
        return base, self.git("rev-parse", "HEAD")

    def test_workspace_is_stable_private_and_repository_scoped(self):
        first = self.workspace()
        second = self.workspace()
        self.assertEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertEqual(os.path.commonpath((first, self.host_temp)), str(self.host_temp))
        self.assertNotEqual(os.path.commonpath((first, self.repo)), str(self.repo))
        self.assertFalse((self.repo / ".skillquiver").exists())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(first.stat().st_mode) & 0o077, 0)

    def test_repository_local_temporary_root_is_rejected(self):
        unsafe_temp = self.repo / "attacker-temp"
        unsafe_temp.mkdir()
        environment = self.environment.copy()
        environment.update({
            "TMPDIR": str(unsafe_temp),
            "TMP": str(unsafe_temp),
            "TEMP": str(unsafe_temp),
        })
        result = subprocess.run(
            [sys.executable, str(ARTIFACT), "workspace", str(self.repo.resolve()), "plan-abc"],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be outside the repository", result.stderr)

    @unittest.skipIf(os.name == "nt", "Windows reparse coverage uses a junction below")
    def test_repository_symlink_cannot_alias_the_temporary_root(self):
        outside_temp = self.root / "outside-temp"
        outside_temp.mkdir()
        unsafe_alias = self.repo / "attacker-temp"
        unsafe_alias.symlink_to(outside_temp, target_is_directory=True)
        environment = self.environment.copy()
        environment["TMPDIR"] = str(unsafe_alias)
        result = subprocess.run(
            [sys.executable, str(ARTIFACT), "workspace", str(self.repo.resolve()), "plan-abc"],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be outside the repository", result.stderr)

    @unittest.skipIf(os.name == "nt", "Windows reparse coverage uses a junction below")
    def test_linked_repository_parent_is_rejected_without_touching_target(self):
        target = self.root / "outside"
        target.mkdir()
        marker = target / "marker.txt"
        marker.write_text("unchanged\n", encoding="utf-8")
        (self.repo / ".skillquiver").symlink_to(target, target_is_directory=True)
        result = self.run_script(WORKSPACE, self.plan)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged\n")
        self.assertFalse((target / "sdd").exists())

    @unittest.skipIf(os.name == "nt", "Windows reparse coverage uses a junction below")
    def test_linked_legacy_gitignore_is_rejected_without_truncation(self):
        legacy = self.repo / ".skillquiver" / "sdd"
        legacy.mkdir(parents=True)
        target = self.root / "outside-gitignore"
        target.write_text("keep me\n", encoding="utf-8")
        (legacy / ".gitignore").symlink_to(target)
        result = self.run_script(WORKSPACE, self.plan)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

    @unittest.skipIf(os.name == "nt", "Windows reparse coverage uses a junction below")
    def test_linked_task_leaf_and_explicit_output_are_rejected(self):
        workspace = self.workspace()
        target = self.root / "outside-brief"
        target.write_text("keep me\n", encoding="utf-8")
        linked_default = workspace / "task-1-brief.md"
        linked_default.symlink_to(target)
        default_result = self.run_script(TASK_BRIEF, self.plan, 1)
        self.assertNotEqual(default_result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

        linked_default.unlink()
        explicit = self.root / "explicit-brief.md"
        explicit.symlink_to(target)
        explicit_result = self.run_script(TASK_BRIEF, self.plan, 1, explicit)
        self.assertNotEqual(explicit_result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

        outside_directory = self.root / "outside-directory"
        outside_directory.mkdir()
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(outside_directory, target_is_directory=True)
        parent_result = self.run_script(
            TASK_BRIEF, self.plan, 1, linked_parent / "brief.md"
        )
        self.assertNotEqual(parent_result.returncode, 0)
        self.assertFalse((outside_directory / "brief.md").exists())

    def test_non_numeric_task_number_cannot_change_the_output_path(self):
        result = self.run_script(TASK_BRIEF, self.plan, "../outside")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TASK_NUMBER must contain only decimal digits", result.stderr)
        self.assertFalse(list(self.host_temp.rglob("task-*")))

    @unittest.skipIf(os.name == "nt", "Windows reparse coverage uses a junction below")
    def test_linked_review_leaf_is_rejected_without_truncation(self):
        base, head = self.commit_range()
        workspace = self.workspace()
        target = self.root / "outside-review"
        target.write_text("keep me\n", encoding="utf-8")
        output = workspace / f"review-{base[:7]}..{head[:7]}.diff"
        output.symlink_to(target)
        result = self.run_script(REVIEW_PACKAGE, self.plan, base, head)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

    def test_legitimate_brief_and_review_outputs_remain_available(self):
        brief_result = self.run_script(TASK_BRIEF, self.plan, 2)
        self.assertEqual(brief_result.returncode, 0, brief_result.stderr)
        brief = self.workspace() / "task-2-brief.md"
        self.assertEqual(brief.read_text(encoding="utf-8"), "## Task 2\nSecond requirement.\n")

        base, head = self.commit_range()
        review_result = self.run_script(REVIEW_PACKAGE, self.plan, base, head)
        self.assertEqual(review_result.returncode, 0, review_result.stderr)
        review = self.workspace() / f"review-{base[:7]}..{head[:7]}.diff"
        content = review.read_text(encoding="utf-8")
        self.assertIn(f"# Review package: {base}..{head}", content)
        self.assertIn("change.txt", content)
        self.assertIn("reviewed change", content)

    @unittest.skipUnless(os.name == "nt", "Windows-only junction regression")
    def test_windows_repository_junction_is_rejected(self):
        target = self.root / "outside-junction"
        target.mkdir()
        junction = self.repo / ".skillquiver"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
        )
        if created.returncode:
            self.skipTest(f"junction creation unavailable: {created.stderr or created.stdout}")
        result = subprocess.run(
            [sys.executable, str(ARTIFACT), "workspace", str(self.repo.resolve()), "plan-abc"],
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
