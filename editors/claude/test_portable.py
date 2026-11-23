"""Exercise the shipped local CLI from a fresh extracted Claude package."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile

EDITOR = Path(__file__).resolve().parent
ROOT = EDITOR.parents[1]
SKILLS = {
    "devsuite", "devsuite-planning", "devsuite-implement", "devsuite-debug",
    "devsuite-review", "devsuite-test", "devsuite-handoff", "devsuite-git",
}


class ClaudePackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="devsuite-claude-tests-")
        cls.addClassCleanup(temp.cleanup)
        cls.folder = Path(temp.name)
        cls.package = cls.folder / "package.zip"
        subprocess.run([sys.executable, "-I", "-B", str(EDITOR / "package.py"),
                        "--out", str(cls.package)], check=True, capture_output=True)

    def extract(self):
        with tempfile.TemporaryDirectory(prefix="devsuite-claude-package-", dir=self.folder) as path:
            output = Path(path)
            with zipfile.ZipFile(self.package) as archive:
                archive.extractall(output)
            yield output

    def run_cli(self, location, *arguments):
        return subprocess.run([sys.executable, "-I", "-B", str(location / "scripts/devsuite.py"),
                               *arguments], cwd=self.folder, capture_output=True, text=True,
                              encoding="utf-8")

    def test_review_modes_ship_and_shared_files_match_studio_hashes(self):
        import hashlib
        pinned = json.loads((ROOT / "references/consolidation/shared-review-modes.json").read_text(encoding="utf-8"))["files"]
        with zipfile.ZipFile(self.package) as archive:
            names = set(archive.namelist())
            for mode in ("code", "request", "receive", "decide", "verify", "release", "gate"):
                self.assertIn(f"skills/devsuite-review/modes/{mode}.md", names)
            for name, digest in pinned.items():
                data = archive.read(f"skills/devsuite-review/modes/{name}").decode("utf-8").replace("\r\n", "\n")
                self.assertEqual(hashlib.sha256(data.encode("utf-8")).hexdigest(), digest, name)
            self.assertFalse([n for n in names if "devsuite-quality" in n])

    def test_planning_modes_ship_and_match_studio_hashes(self):
        import hashlib
        pinned = json.loads((ROOT / "references/consolidation/shared-plan-modes.json").read_text(encoding="utf-8"))["files"]
        with zipfile.ZipFile(self.package) as archive:
            names = set(archive.namelist())
            for name, digest in pinned.items():
                path = f"skills/devsuite-planning/modes/{name}"
                self.assertIn(path, names)
                data = archive.read(path).decode("utf-8").replace("\r\n", "\n")
                self.assertEqual(hashlib.sha256(data.encode("utf-8")).hexdigest(), digest, name)
            guide = archive.read("skills/devsuite-planning/SKILL.md").decode("utf-8")
            self.assertIn('<a id="mode-map"></a>', guide)
            self.assertNotIn("## Optional task-boundary board", guide)
            self.assertFalse([n for n in names if "companion" in n])

    def test_execution_modes_and_subagent_tree_ship_and_match_studio_hashes(self):
        import hashlib
        pinned = json.loads((ROOT / "references/consolidation/shared-execute-modes.json").read_text(encoding="utf-8"))["files"]
        with zipfile.ZipFile(self.package) as archive:
            names = set(archive.namelist())
            for name, digest in pinned.items():
                path = f"skills/devsuite-implement/modes/{name}"
                self.assertIn(path, names)
                data = archive.read(path).decode("utf-8").replace("\r\n", "\n")
                self.assertEqual(hashlib.sha256(data.encode("utf-8")).hexdigest(), digest, name)
            for script in ("review-package", "sdd-workspace", "task-brief"):
                self.assertIn(f"skills/devsuite-implement/modes/subagent/scripts/{script}", names)

    def test_git_skill_ships_with_modes_and_guardrail_scripts(self):
        import hashlib
        pinned = json.loads((ROOT / "references/consolidation/shared-git-modes.json").read_text(encoding="utf-8"))["files"]
        with zipfile.ZipFile(self.package) as archive:
            names = set(archive.namelist())
            self.assertIn("skills/devsuite-git/SKILL.md", names)
            for name, digest in pinned.items():
                path = f"skills/devsuite-git/modes/{name}"
                self.assertIn(path, names)
                data = archive.read(path).decode("utf-8").replace("\r\n", "\n")
                self.assertEqual(hashlib.sha256(data.encode("utf-8")).hexdigest(), digest, name)

    def test_debug_modes_and_resources_ship_and_match_studio_hashes(self):
        import hashlib
        pinned = json.loads((ROOT / "references/consolidation/shared-debug-modes.json").read_text(encoding="utf-8"))["files"]
        with zipfile.ZipFile(self.package) as archive:
            names = set(archive.namelist())
            for name, digest in pinned.items():
                path = f"skills/devsuite-debug/modes/{name}"
                self.assertIn(path, names)
                data = archive.read(path).decode("utf-8").replace("\r\n", "\n")
                self.assertEqual(hashlib.sha256(data.encode("utf-8")).hexdigest(), digest, name)
            for script in ("diagnose/scripts/find-polluter.sh", "loop/scripts/hitl-loop.template.sh"):
                self.assertIn(f"skills/devsuite-debug/modes/{script}", names)

    def test_build_is_reproducible_and_package_only_has_local_components(self):
        other = self.folder / "rebuild.zip"
        subprocess.run([sys.executable, "-I", "-B", str(EDITOR / "package.py"),
                        "--out", str(other)], check=True, capture_output=True)
        self.assertEqual(self.package.read_bytes(), other.read_bytes())
        with zipfile.ZipFile(self.package) as archive:
            entries = archive.namelist()
            self.assertEqual(entries, sorted(set(entries)))
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read(".claude-plugin/plugin.json"))
            upstream = json.loads((ROOT / ".codex-plugin/plugin.json").read_bytes())
            for field in ("name", "version"):
                self.assertEqual(manifest[field], upstream[field])
            for field in ("mcpServers", "apps", "hooks", "commands", "agents"):
                self.assertNotIn(field, manifest)
            self.assertEqual({name.split("/")[1] for name in entries if name.startswith("skills/")}, SKILLS)
            forbidden = {".app.json", ".codex-plugin", ".mcp.json", "hooks", "agents", "commands"}
            self.assertFalse(any(forbidden.intersection(Path(name).parts) for name in entries))
            self.assertEqual(archive.read("runtime/routing.py"), (ROOT / "runtime/routing.py").read_bytes())
            self.assertEqual(archive.read("assets/logo.png"), (ROOT / "assets/logo.png").read_bytes())

    def test_doctor_verifies_actual_files_and_rejects_integration_or_missing_references(self):
        for location in self.extract():
            healthy = self.run_cli(location, "doctor")
            report = json.loads(healthy.stdout)
            self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(set(report["workflows"]), SKILLS)
            self.assertFalse(report["providersExecuted"])
            (location / ".mcp.json").write_text("{}", encoding="utf-8")
            (location / "references/platforms.md").unlink()
            broken = self.run_cli(location, "doctor")
            report = json.loads(broken.stdout)
            self.assertEqual(broken.returncode, 1)
            self.assertEqual(report["status"], "FAIL")
            self.assertTrue(any(".mcp.json" in message for message in report["errors"]))
            self.assertTrue(any("platforms.md" in message for message in report["errors"]))

    def test_router_selects_portable_guides_without_executing_input(self):
        for location in self.extract():
            cases = (
                ("Review the local diff", "devsuite-review"),
                ("Gennemgå den lokale diff", "devsuite-review"),
                ("Plan the test approach", "devsuite-test"),
                ("Run a lintkontrol", "devsuite-review"),
                ("Review the GitHub Actions config file", "devsuite-review"),
                ("Don't use GitHub, review the local diff", "devsuite-review"),
                ("Review the literal request $(touch claude-injection-probe)", "devsuite-review"),
            )
            for request, skill in cases:
                with self.subTest(request=request):
                    process = self.run_cli(location, "route", "--", request)
                    report = json.loads(process.stdout)
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    self.assertEqual((report["status"], report["skill"]), ("READY", skill))
                    self.assertTrue((location / report["entrypoint"]).is_file())
                    self.assertFalse(report["executionAuthorized"])
                    self.assertFalse(report["externalActionAuthorized"])
                    self.assertFalse(report["providersExecuted"])
            self.assertFalse((self.folder / "claude-injection-probe").exists())

    def test_router_preserves_provider_scope_and_reports_unavailability(self):
        for location in self.extract():
            cases = (
                ("Find mine gemte Codex cloud-miljøer", "devsuite-tasks"),
                ("Start a Codex task to test this change", "devsuite-tasks"),
                ("Run the build in the background", "devsuite-jobs"),
                ("Convert this PDF through CloudConvert", "devsuite-convert"),
                ("Request a Codex Security deep security scan", "devsuite-security-assess"),
                ("Review pull request 14 on GitHub.", "devsuite-review"),
                ("Review https://github.com/example/project/pull/14", "devsuite-review"),
            )
            for request, skill in cases:
                with self.subTest(request=request):
                    process = self.run_cli(location, "route", "--", request)
                    report = json.loads(process.stdout)
                    self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
                    self.assertEqual((report["status"], report["requestedSkill"]), ("UNAVAILABLE", skill))
                    self.assertNotIn("entrypoint", report)
                    self.assertFalse(report["executionAuthorized"])
                    self.assertFalse(report["externalActionAuthorized"])
                    self.assertFalse(report["providersExecuted"])


if __name__ == "__main__":
    unittest.main()
