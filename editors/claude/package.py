"""Build the Claude Code DevSuite ZIP from shared local workflows."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

EDITOR = Path(__file__).resolve().parent
ROOT = EDITOR.parents[1]
sys.path.insert(0, str(ROOT))
from runtime.routing import LOCAL_WORKFLOWS


LICENSE_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "licenses/coordinator.txt",
    "licenses/guardrails-1.txt",
    "licenses/guardrails.txt",
    "licenses/workflows.txt",
)
LOCAL_FILES = (
    "assets/logo.png",
    "runtime/__init__.py",
    "runtime/routing.py",
    "references/policy.md",
    "references/engineering-decisions.md",
    "references/verification-matrix.md",
    "references/platforms.md",
    "references/platforms/flutter.md",
    "references/platforms/javascript-typescript.md",
    "references/platforms/laravel-php.md",
    "references/platforms/python.md",
)
MARKDOWN_CHANGES = {
    "devsuite-review": (
        ("For provider PRs compare head/base with local HEAD, preserve edits and report the actual revision.",
         "Use locally available refs, preserve edits and report the actual revision."),
    ),
    "devsuite-test": (
        (" [Environment](../devsuite-environment/SKILL.md) handles desktop and handoffs.", ""),
    ),
    "devsuite-handoff": (
        (" When board coordination is enabled, release own exact completed/stopped claim or explicitly retain "
         "bounded unfinished ownership; time or silence never proves another owner stale.", ""),
        ("Use the integrated [process jobs](../devsuite-jobs/SKILL.md) for local workload lifecycle and "
         "[Codex Tasks](../devsuite-tasks/SKILL.md) for registered remote tasks. Discover actual capabilities "
         "and retain existing handles; never restart from a missing receipt alone.",
         "Retain existing work handles and never restart from a missing receipt alone. Record remote work "
         "as unverified when the original host is unavailable."),
    ),
}


def local_guide(name):
    if name == "devsuite":
        return (EDITOR / "SKILL.md").read_bytes()
    file = ROOT / "skills" / name / "SKILL.md"
    if name != "devsuite-planning" and name not in MARKDOWN_CHANGES:
        return file.read_bytes()
    text = file.read_text(encoding="utf-8")
    if name == "devsuite-planning":
        local, separator, _ = text.partition("\n## Optional task-boundary board\n")
        if not separator:
            raise ValueError("Review the Claude planning cutoff after changing the source guide")
        return (local.rstrip() + "\n").encode("utf-8")
    for old, new in MARKDOWN_CHANGES[name]:
        if text.count(old) != 1:
            raise ValueError(f"Review the Claude adaptation of {name} after changing the source guide")
        text = text.replace(old, new)
    return text.encode("utf-8")


def package(output):
    if output.suffix.lower() != ".zip":
        raise ValueError("Output must be a ZIP file")
    manifest_path = ".claude-plugin/plugin.json"
    manifest = (EDITOR / manifest_path).read_bytes()
    claude = json.loads(manifest)
    codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_bytes())
    if (claude["name"], claude["version"]) != (codex["name"], codex["version"]):
        raise ValueError("Claude and Codex source metadata must agree on product and release")
    files = {
        **{name: (ROOT / name).read_bytes() for name in (*LICENSE_FILES, *LOCAL_FILES)},
        **{f"skills/{name}/SKILL.md": local_guide(name) for name in LOCAL_WORKFLOWS},
        # Review mode files ship unchanged; the shared ones must stay byte-identical with Studio.
        **{f"skills/devsuite-review/modes/{mode.name}": mode.read_bytes()
           for mode in sorted((ROOT / "skills" / "devsuite-review" / "modes").glob("*.md"))},
        manifest_path: manifest,
        "scripts/devsuite.py": (EDITOR / "devsuite.py").read_bytes(),
        "README.md": (EDITOR / "README.md").read_bytes(),
    }
    with zipfile.ZipFile(output, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    print(json.dumps({"version": claude["version"], "files": len(files), "path": str(output),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=lambda value: Path(value).resolve(), required=True)
    args = parser.parse_args()
    package(args.out)
