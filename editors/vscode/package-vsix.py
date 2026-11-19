"""Use the exact native DevSuite package in a reproducible, local VS Code VSIX."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

EDITOR = Path(__file__).resolve().parent
REPO = EDITOR.parents[1]
EXTENSION_FILES = ("README.md", "extension.cjs", "package.json", "runner.cjs")
STAMP = (1980, 1, 1, 0, 0, 0)


def native_payload(temporary):
    archive = temporary / "devsuite-native.zip"
    subprocess.run([sys.executable, "-I", "-B", str(REPO / "scripts/package.py"), "--out", str(archive)],
                   cwd=REPO, stdin=subprocess.DEVNULL, check=True, stdout=subprocess.DEVNULL)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.namelist()
        if len(members) != len(set(members)) or bundle.testzip():
            raise ValueError("The native DevSuite archive failed ZIP integrity checks.")
        for name in members:
            if ("\\" in name or ":" in name or name.startswith("/") or any(
                    part in ("", ".", "..") for part in name.split("/"))):
                raise ValueError("Unexpected native archive member: " + name)
        payload = {name: bundle.read(name) for name in members}
    manifest = json.loads(payload[".codex-plugin/plugin.json"])
    logo_path = PurePosixPath(manifest["interface"]["logo"]).as_posix()
    stage = {"core/" + name: data for name, data in payload.items()}
    return manifest["version"], stage | {"icon.png": payload[logo_path], "LICENSE": payload["LICENSE"]}


def build(vsce, output):
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is required by Microsoft's VS Code packager.")
    if output.suffix.lower() != ".vsix":
        raise ValueError("The output must be a .vsix file.")
    with tempfile.TemporaryDirectory(prefix="wrp-devsuite-vsix-") as directory:
        temporary = Path(directory)
        stage = temporary / "extension"
        version, stage_files = native_payload(temporary)
        stage_files.update({name: (EDITOR / name).read_bytes() for name in EXTENSION_FILES})
        manifest = json.loads(stage_files["package.json"])
        if manifest["version"] != version:
            raise ValueError("The extension and bundled DevSuite versions must match.")
        for name, data in stage_files.items():
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        raw = temporary / "raw.vsix"
        subprocess.run([node, str(vsce), "package", "--no-dependencies",
                        "--no-rewrite-relative-links", "--out", str(raw)],
                       cwd=stage, stdin=subprocess.DEVNULL, check=True)
        with zipfile.ZipFile(raw) as bundle:
            names = bundle.namelist()
            metadata = {"extension.vsixmanifest", "[Content_Types].xml"}
            packaged_names = {"README.md": "readme.md", "LICENSE": "LICENSE.txt"}
            archive_paths = {name: "extension/" + packaged_names.get(name, name) for name in stage_files}
            expected = metadata | set(archive_paths.values())
            if len(names) != len(expected) or set(names) != expected or bundle.testzip():
                raise ValueError("Unexpected VSIX members: "
                                 + repr({"missing": sorted(expected - set(names)),
                                         "extra": sorted(set(names) - expected)}))
            for name, data in stage_files.items():
                if bundle.read(archive_paths[name]) != data:
                    raise ValueError("VSIX payload changed during official packaging: " + name)
            xml = ET.fromstring(bundle.read("extension.vsixmanifest"))
            identity = xml.find(".//{*}Identity")
            if identity is None or (identity.get("Id"), identity.get("Publisher"), identity.get("Version")) != (
                    manifest["name"], manifest["publisher"], version):
                raise ValueError("The VSIX identity differs from extension/package.json.")
            with zipfile.ZipFile(output, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as release:
                for name in sorted(names):
                    info = zipfile.ZipInfo(name, STAMP)
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    release.writestr(info, bundle.read(name), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    print(json.dumps({"version": version, "files": len(expected), "path": str(output),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vsce", type=lambda value: Path(value).resolve(strict=True), required=True,
                        help="Existing @vscode/vsce 3.7.1 CLI. The build does not download it.")
    parser.add_argument("--out", type=lambda value: Path(value).resolve(), required=True)
    args = parser.parse_args()
    vsce_package = json.loads((args.vsce.parent / "package.json").read_bytes())
    if vsce_package.get("name") != "@vscode/vsce" or vsce_package.get("version") != "3.7.1":
        parser.error("--vsce must refer to the @vscode/vsce 3.7.1 CLI script.")
    build(args.vsce, args.out)
