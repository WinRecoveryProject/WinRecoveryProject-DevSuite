"""Union and integrity guarantees for the consolidated devsuite-git skill."""
from __future__ import annotations

import hashlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.routing import route  # noqa: E402
from tests.test_review_consolidation import check_matrix, read_json  # noqa: E402

CONSOLIDATION = ROOT / "references" / "consolidation"
MODES = ROOT / "skills" / "devsuite-git" / "modes"
PINNED = ("worktree.md", "finish.md", "conflicts.md", "safety.md",
          "safety/scripts/block-dangerous-git.sh", "safety/scripts/classify_git_command.py")


class GitConsolidationTests(unittest.TestCase):
    def test_pinned_git_modes_match_hashes(self):
        pinned = read_json(CONSOLIDATION / "shared-git-modes.json")["files"]
        for name in PINNED:
            self.assertIn(name, pinned)
        for name, digest in pinned.items():
            data = (MODES / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)

    def test_pinned_git_modes_have_no_package_links(self):
        for name in read_json(CONSOLIDATION / "shared-git-modes.json")["files"]:
            if not name.endswith(".md"):
                continue
            text = re.sub(r'<a id="[^"]*"></a>', "", (MODES / name).read_text(encoding="utf-8"))
            self.assertNotRegex(text, r"\]\(\.\./\.\./|skills/|studio-|policy\.md", name)

    def test_git_matrix_union(self):
        matrix = read_json(CONSOLIDATION / "git-matrix.json")
        self.assertEqual(check_matrix(matrix), [])
        self.assertFalse([e["id"] for e in matrix["entries"] if e.get("status") == "dropped"])

    def test_removed_git_triggers_route(self):
        matrix = read_json(CONSOLIDATION / "git-matrix.json")
        misses = []
        for entry in matrix["entries"]:
            target = entry.get("target") or ""
            if entry["kind"] != "trigger" or "skills/devsuite-git/SKILL.md#mode-" not in target:
                continue
            want = target.split("#mode-", 1)[1]
            result = route(entry["text"])
            if (result["skill"], result["mode"]) != ("devsuite-git", want):
                misses.append(f"{entry['id']}: got {result['skill']}/{result['mode']}, want devsuite-git/{want}")
        self.assertEqual(misses, [])

    def test_git_requests_route_to_devsuite_git(self):
        for request, mode in (("fix this merge conflict", "conflicts"), ("block a force push to main", "safety")):
            result = route(request)
            self.assertEqual((result["skill"], result["mode"]), ("devsuite-git", mode), request)

    def test_worktree_setup_requires_separate_execution_authority(self):
        text = (MODES / "worktree.md").read_text(encoding="utf-8")
        self.assertIn("Consent to create or use a worktree authorizes checkout isolation only.", text)
        self.assertIn("Manifest presence tells you what to inspect, never what to execute.", text)
        self.assertIn("If the existing request does not authorize every required effect, stop and ask", text)
        self.assertIn("A worktree-only request must not run tests.", text)
        self.assertIn("do not run setup or tests solely because of the fallback", text)
        for executable_path in ("lifecycle scripts", "`build.rs`", "build backends", "test commands"):
            self.assertIn(executable_path, text)
        for automatic_sink in (
            "if [ -f package.json ]; then npm install; fi",
            "if [ -f Cargo.toml ]; then cargo build; fi",
            "if [ -f requirements.txt ]; then pip install -r requirements.txt; fi",
            "then poetry install",
            "then pip install -e .; fi",
            "if [ -f go.mod ]; then go mod download; fi",
            "Then run setup and baseline tests in place.",
        ):
            self.assertNotIn(automatic_sink, text)


if __name__ == "__main__":
    unittest.main()
