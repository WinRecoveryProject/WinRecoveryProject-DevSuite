#!/usr/bin/env python3
"""Dry-run-first lifecycle changes for Coordinator project markers.

Schema 2 lifecycle operations create, pin, poll, or stop no Coordinator task
and own no heartbeat, schedule, or Mission Control runtime. Legacy schema-1
cleanup actions are reported only when old project state proves that those
components may still exist.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DISCOVERY_HEADING = "## Codex task-boundary board"
DISCOVERY_BLOCK = """## Codex task-boundary board

- This repository uses the opt-in Codex task-boundary board in `.codex/coordination/project.yaml`.
- Before substantial writes, load the installed `devsuite-planning` skill, list active claims from the primary worktree, and publish only this task's bounded claim.
- Native Codex tasks remain the execution, messaging, and transcript authority; an explicitly requested goal Coordinator is on demand, with no heartbeat or mandatory pull-request workflow.
- Reject cross-project notices and never store transcripts, reasoning, prompts, or tool output in Coordinator state."""

LEGACY_DISCOVERY_BLOCKS = (
    # Recognize the previous schema-2 discovery block when an authorized lifecycle
    # change adopts this runtime; preserve all unrelated AGENTS.md bytes.
    DISCOVERY_BLOCK.replace("`devsuite-planning`", "`codex-coordinator`"),
    """## Codex Coordinator

- This repository is Codex Coordinator-enabled.
- Project identity is in `.codex/coordination/project.yaml`; current coordination state is in `.codex/coordination/CURRENT.md`.
- Load the globally installed `codex-coordinator` skill at the start of every task in this repository; all same-repository tasks are managed by default unless the user explicitly excludes one.
- Respect the project ID and assigned task boundary; reject missing or mismatched cross-thread project bindings.
- Treat Coordinator internals as protected; only an explicitly user-authorised `COORDINATOR_MAINTAINER` may modify them.""",
    """## Codex Coordinator

- This repository is Codex Coordinator-enabled.
- Project identity is in `.codex/coordination/project.yaml`; current coordination state is in `.codex/coordination/CURRENT.md`.
- Load the globally installed `codex-coordinator` skill before substantial, overlapping, parallel, or cross-thread work.
- Respect the project ID and assigned task boundary; reject missing or mismatched cross-thread project bindings.
- Treat Coordinator internals as protected; only an explicitly user-authorised `COORDINATOR_MAINTAINER` may modify them.""",
)

IGNORE_BLOCK = ".codex/coordination/*\n!.codex/coordination/project.yaml"
QUARANTINE_NAME = "coordination.codex-coordinator-purge"
MARKER_KEYS = ("schema_version", "coordination_enabled", "project_id")
TASK_PREFIX = re.compile(r"[A-Z][A-Z0-9-]{0,15}")
MIGRATION_BACKUP_NAME = "project.schema-1.yaml"
MAX_LEGACY_FILES = 10_000
MAX_TASK_TAIL_BYTES = 4096
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
TERMINAL_TASK_STATUSES = {
    "CANCELED",
    "CANCELLED",
    "COMPLETE",
    "COMPLETED",
    "STOPPED",
    "SUPERSEDED",
}


class LifecycleError(RuntimeError):
    """Raised when a lifecycle precondition is unsafe or ambiguous."""


@dataclass(frozen=True)
class Document:
    path: Path
    project_root: Path
    raw: bytes
    text: str
    newline: str
    bom: bool
    existed: bool = True
    identity: tuple[int, int] | None = None

    def encode(self, text: str) -> bytes:
        payload = text.encode("utf-8")
        return (b"\xef\xbb\xbf" + payload) if self.bom else payload


@dataclass(frozen=True)
class Project:
    root: Path
    coordination: Path
    marker: Document
    fields: dict[str, str]
    resumed_purge: bool

    @property
    def project_id(self) -> str:
        return self.fields["project_id"]

    @property
    def enabled(self) -> bool:
        return self.fields["coordination_enabled"] == "true"

    @property
    def schema(self) -> int:
        return int(self.fields["schema_version"])


def _metadata_is_linklike(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_file_identity(left, right) and (
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _require_document_location(project_root: Path, path: Path) -> None:
    root_metadata = _lstat(project_root)
    if (
        root_metadata is None
        or _metadata_is_linklike(root_metadata)
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        raise LifecycleError(
            f"project root must be a physical directory for document access: {project_root}"
        )
    try:
        relative = path.relative_to(project_root)
    except ValueError as error:
        raise LifecycleError(f"document escapes the project root: {path}") from error
    if not relative.parts or ".." in relative.parts:
        raise LifecycleError(f"document path is not a contained file: {path}")

    current = project_root
    for component in relative.parts[:-1]:
        current /= component
        metadata = _lstat(current)
        if metadata is None:
            break
        if _metadata_is_linklike(metadata):
            raise LifecycleError(
                f"document ancestor must not be a symlink, junction, or reparse point: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise LifecycleError(f"document ancestor must be a directory: {current}")


def _read_document(
    path: Path, *, project_root: Path, required: bool = True
) -> Document | None:
    _require_document_location(project_root, path)
    initial = _lstat(path)
    if initial is None:
        if required:
            raise LifecycleError(f"required file is missing: {path}")
        return None
    if _metadata_is_linklike(initial):
        raise LifecycleError(
            f"document must not be a symlink, junction, or reparse point: {path}"
        )
    if not stat.S_ISREG(initial.st_mode):
        raise LifecycleError(f"expected a file: {path}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor: int | None = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise LifecycleError(
                f"document must not be a symlink, junction, or reparse point: {path}"
            ) from error
        raise
    try:
        opened = os.fstat(descriptor)
        current = _lstat(path)
        _require_document_location(project_root, path)
        # Windows has no O_NOFOLLOW; matching the opened handle to lstat before
        # reading prevents a raced reparse target from contributing any bytes.
        if (
            current is None
            or _metadata_is_linklike(current)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not _same_file_identity(initial, opened)
            or not _same_file_identity(opened, current)
        ):
            raise LifecycleError(f"document changed while it was being opened: {path}")

        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            before_read = os.fstat(stream.fileno())
            raw = stream.read()
            after_read = os.fstat(stream.fileno())
        final = _lstat(path)
        _require_document_location(project_root, path)
        if (
            final is None
            or _metadata_is_linklike(final)
            or not _same_file_snapshot(before_read, after_read)
            or not _same_file_snapshot(after_read, final)
        ):
            raise LifecycleError(f"document changed while it was being read: {path}")
    finally:
        if descriptor is not None:
            os.close(descriptor)

    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise LifecycleError(f"file is not UTF-8: {path}") from error
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return Document(
        path=path,
        project_root=project_root,
        raw=raw,
        text=text,
        newline=newline,
        bom=bom,
        existed=True,
        identity=(after_read.st_dev, after_read.st_ino),
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _is_linklike(path: Path) -> bool:
    metadata = _lstat(path)
    return metadata is not None and _metadata_is_linklike(metadata)


def _require_contained_directory(root: Path, path: Path, *, label: str) -> None:
    if _is_linklike(path):
        raise LifecycleError(f"{label} must not be a symlink or junction: {path}")
    if path.exists() and not path.is_dir():
        raise LifecycleError(f"{label} must be a directory: {path}")
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise LifecycleError(f"{label} escapes the project root: {path}") from error


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise LifecycleError(f"Git validation failed for {root}")
    return result.stdout


def _validate_primary_worktree(root: Path) -> None:
    top = Path(_git_output(root, "rev-parse", "--show-toplevel").strip())
    if not _same_path(root, top):
        raise LifecycleError("project root must equal the Git worktree root")
    listing = _git_output(root, "worktree", "list", "--porcelain").splitlines()
    first = next((line[9:] for line in listing if line.startswith("worktree ")), None)
    if not first or not _same_path(root, Path(first)):
        raise LifecycleError("lifecycle changes must run from the primary Git worktree")


def _parse_marker(document: Document) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key in MARKER_KEYS:
        matches = re.findall(rf"(?m)^{re.escape(key)}:\s*([^\r\n#]+?)\s*$", document.text)
        if len(matches) != 1:
            raise LifecycleError(f"marker must contain exactly one {key}")
        fields[key] = matches[0].strip()
    if fields["schema_version"] not in {"1", "2"}:
        raise LifecycleError("only marker schema 1 and 2 are supported")
    if fields["coordination_enabled"] not in {"true", "false"}:
        raise LifecycleError("coordination_enabled must be true or false")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", fields["project_id"]):
        raise LifecycleError("project_id is invalid")
    return fields


def _marker_scalar(
    document: Document, key: str, *, required: bool = False
) -> str | None:
    matches = re.findall(
        rf"(?m)^\s*{re.escape(key)}:\s*([^\r\n#]+?)\s*$", document.text
    )
    if len(matches) > 1:
        raise LifecycleError(f"marker contains duplicate {key}")
    if not matches:
        if required:
            raise LifecycleError(f"schema-1 marker is missing {key}")
        return None
    return matches[0].strip()


def _validate_schema_one_migration(document: Document) -> None:
    expected = {
        "current": ".codex/coordination/CURRENT.md",
        "tasks": ".codex/coordination/tasks",
        "cross_project_task_access": "false",
        "cross_project_state_changes": "false",
    }
    for key, value in expected.items():
        if _marker_scalar(document, key, required=True) != value:
            raise LifecycleError(f"schema-1 marker has incompatible {key}")


def _schema_two_marker(project: Project) -> str:
    _validate_schema_one_migration(project.marker)
    lines = [
        "schema_version: 2",
        "coordination_enabled: false",
        f"project_id: {project.project_id}",
    ]
    for key in ("project_name", "task_prefix"):
        value = _marker_scalar(project.marker, key)
        if value is not None:
            lines.append(f"{key}: {value}")
    lines.extend(
        [
            "canonical_paths:",
            "  active: .codex/coordination/active",
            "  archive: .codex/coordination/archive",
            "access:",
            "  cross_project_task_access: false",
            "  cross_project_state_changes: false",
            "",
        ]
    )
    return project.marker.newline.join(lines)


def _load_project(root_value: str, *, allow_resumed_purge: bool = False) -> Project:
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise LifecycleError(f"project root is not a directory: {root}")
    _validate_primary_worktree(root)
    codex_directory = root / ".codex"
    coordination = codex_directory / "coordination"
    quarantine = codex_directory / QUARANTINE_NAME
    _require_contained_directory(root, codex_directory, label=".codex directory")
    _require_contained_directory(root, coordination, label="coordination directory")
    _require_contained_directory(root, quarantine, label="purge quarantine")
    resumed = False
    if not coordination.exists() and allow_resumed_purge and quarantine.is_dir():
        coordination = quarantine
        resumed = True
    elif quarantine.exists():
        raise LifecycleError(f"purge quarantine already exists: {quarantine}")
    marker = _read_document(coordination / "project.yaml", project_root=root)
    assert marker is not None
    return Project(root, coordination, marker, _parse_marker(marker), resumed)


def _replace_marker_enabled(document: Document, enabled: bool) -> str:
    replacement = "true" if enabled else "false"
    updated, count = re.subn(
        r"(?m)^coordination_enabled:\s*(?:true|false)\s*$",
        f"coordination_enabled: {replacement}",
        document.text,
    )
    if count != 1:
        raise LifecycleError("marker enablement field is ambiguous")
    return updated


def _validate_schema_two_reactivation(document: Document) -> None:
    expected = {
        "active": ".codex/coordination/active",
        "archive": ".codex/coordination/archive",
        "cross_project_task_access": "false",
        "cross_project_state_changes": "false",
    }
    for key, value in expected.items():
        matches = re.findall(
            rf"(?m)^\s*{re.escape(key)}:\s*([^\r\n#]+?)\s*$", document.text
        )
        if len(matches) != 1 or matches[0].strip() != value:
            raise LifecycleError(f"schema-2 marker has incompatible {key}")


def _block_for(document: Document, block: str) -> str:
    return block.replace("\n", document.newline)


def _remove_exact_block(document: Document, block: str, label: str) -> str:
    rendered = _block_for(document, block)
    count = document.text.count(rendered)
    if count > 1:
        raise LifecycleError(f"duplicate {label} blocks found")
    if count == 0:
        if label == "Coordinator discovery" and DISCOVERY_HEADING in document.text:
            raise LifecycleError("Coordinator discovery block differs from the packaged contract")
        return document.text
    start = document.text.index(rendered)
    end = start + len(rendered)
    if start >= len(document.newline) and document.text[:start].endswith(document.newline * 2):
        start -= len(document.newline)
    if document.text[end:].startswith(document.newline):
        end += len(document.newline)
    return document.text[:start] + document.text[end:]


def _remove_discovery_block(document: Document) -> str:
    matches: list[str] = []
    for block in (DISCOVERY_BLOCK, *LEGACY_DISCOVERY_BLOCKS):
        count = document.text.count(_block_for(document, block))
        if count > 1:
            raise LifecycleError("duplicate Coordinator discovery blocks found")
        if count == 1:
            matches.append(block)
    if len(matches) > 1:
        raise LifecycleError("multiple supported Coordinator discovery blocks found")
    if not matches:
        if DISCOVERY_HEADING in document.text:
            raise LifecycleError("Coordinator discovery block differs from the packaged contract")
        return document.text
    return _remove_exact_block(document, matches[0], "supported Coordinator discovery")


def _ensure_current_discovery(
    document: Document | None, path: Path, *, project_root: Path
) -> tuple[Document, str]:
    if document is None:
        return _add_exact_block(
            None,
            path,
            DISCOVERY_BLOCK,
            "Coordinator discovery",
            project_root=project_root,
        )
    if document.text.count(_block_for(document, DISCOVERY_BLOCK)) == 1:
        return document, document.text
    normalized = _remove_discovery_block(document)
    source = Document(
        path=document.path,
        project_root=document.project_root,
        raw=document.raw,
        text=normalized,
        newline=document.newline,
        bom=document.bom,
        existed=document.existed,
        identity=document.identity,
    )
    return _add_exact_block(
        source,
        path,
        DISCOVERY_BLOCK,
        "Coordinator discovery",
        project_root=project_root,
    )


def _add_exact_block(
    document: Document | None,
    path: Path,
    block: str,
    label: str,
    *,
    project_root: Path,
) -> tuple[Document, str]:
    if document is None:
        _require_document_location(project_root, path)
        empty = Document(
            path=path,
            project_root=project_root,
            raw=b"",
            text="",
            newline="\n",
            bom=False,
            existed=False,
        )
        return empty, block + "\n"
    rendered = _block_for(document, block)
    count = document.text.count(rendered)
    if count > 1:
        raise LifecycleError(f"duplicate {label} blocks found")
    if count == 1:
        return document, document.text
    if label == "Coordinator discovery" and DISCOVERY_HEADING in document.text:
        raise LifecycleError("Coordinator discovery block differs from the packaged contract")
    separator = "" if not document.text else (document.newline if document.text.endswith(document.newline) else document.newline * 2)
    if document.text.endswith(document.newline) and not document.text.endswith(document.newline * 2):
        separator = document.newline
    return document, document.text + separator + rendered + document.newline


def _coordinator_id(project: Project) -> str | None:
    current = _read_document(
        project.coordination / "CURRENT.md",
        project_root=project.root,
        required=False,
    )
    if current is None:
        return None
    matches = re.findall(r"(?m)^\*\*Coordinator thread ID:\*\*\s*(\S+)\s*$", current.text)
    return matches[0] if len(matches) == 1 and matches[0] != "NONE" else None


def _bounded_files(path: Path) -> list[Path]:
    if _is_linklike(path):
        raise LifecycleError(f"legacy state must not be a symlink or junction: {path}")
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise LifecycleError(f"legacy state is not a normal file or directory: {path}")
    files: list[Path] = []
    entries = 0
    for child in path.rglob("*"):
        entries += 1
        if entries > MAX_LEGACY_FILES:
            raise LifecycleError(
                f"legacy inventory exceeds {MAX_LEGACY_FILES} entries: {path}"
            )
        if _is_linklike(child):
            raise LifecycleError(
                f"legacy state must not contain a symlink or junction: {child}"
            )
        if child.is_file():
            files.append(child)
    return files


def _tail_text(path: Path) -> str | None:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > MAX_TASK_TAIL_BYTES:
            stream.seek(-MAX_TASK_TAIL_BYTES, os.SEEK_END)
        raw = stream.read(MAX_TASK_TAIL_BYTES)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _legacy_inventory(project: Project) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    task_files: list[Path] = []
    total_files = 0
    total_bytes = 0
    protected_names = {"project.yaml", MIGRATION_BACKUP_NAME, "active", "archive"}
    for path in sorted(project.coordination.iterdir(), key=lambda item: item.name.casefold()):
        if path.name in protected_names:
            continue
        files = _bounded_files(path)
        size = sum(item.stat().st_size for item in files)
        total_files += len(files)
        total_bytes += size
        relative = path.relative_to(project.root).as_posix()
        entries.append(
            {
                "path": relative,
                "kind": "directory" if path.is_dir() else "file",
                "fileCount": len(files),
                "bytes": size,
                "action": "preserve-ignored",
            }
        )
        if path.name == "tasks" and path.is_dir():
            task_files = files

    terminal_counts: dict[str, int] = {}
    unclassified = 0
    for path in task_files:
        tail = _tail_text(path)
        match = (
            re.search(r"(?mi)^\*\*Task status:\*\*\s*([A-Z-]+)\s*$", tail)
            if tail is not None
            else None
        )
        status = match.group(1).upper() if match else None
        if status in TERMINAL_TASK_STATUSES:
            terminal_counts[status] = terminal_counts.get(status, 0) + 1
        else:
            unclassified += 1

    return {
        "entries": entries,
        "fileCount": total_files,
        "bytes": total_bytes,
        "taskRecords": len(task_files),
        "explicitTerminalTaskRecords": sum(terminal_counts.values()),
        "terminalStatuses": dict(sorted(terminal_counts.items())),
        "unclassifiedTaskRecords": unclassified,
        "legacyCoordinatorThreadId": _coordinator_id(project),
        "ownershipImported": False,
    }


def _validate_document_for_write(document: Document) -> None:
    current = _read_document(
        document.path,
        project_root=document.project_root,
        required=False,
    )
    if document.existed:
        if (
            current is None
            or current.identity != document.identity
            or current.raw != document.raw
        ):
            raise LifecycleError(
                f"document changed after lifecycle planning: {document.path}"
            )
    elif current is not None:
        raise LifecycleError(
            f"document appeared after lifecycle planning: {document.path}"
        )


def _atomic_write(document: Document, payload: bytes) -> Document:
    path = document.path
    _require_document_location(document.project_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_document_for_write(document)
        os.replace(temp, path)
        written = _read_document(path, project_root=document.project_root)
        assert written is not None
        return written
    finally:
        temp.unlink(missing_ok=True)


def _apply_documents(changes: Iterable[tuple[Document, str]]) -> None:
    prepared = [(document, document.encode(text)) for document, text in changes if document.raw != document.encode(text)]
    written: list[tuple[Document, Document]] = []
    try:
        for document, payload in prepared:
            replacement = _atomic_write(document, payload)
            written.append((document, replacement))
    except Exception:
        for document, replacement in reversed(written):
            if document.existed:
                _atomic_write(replacement, document.raw)
            else:
                _validate_document_for_write(replacement)
                document.path.unlink(missing_ok=True)
        raise


def _initialization_root(root_value: str) -> tuple[Path, Path]:
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise LifecycleError(f"project root is not a directory: {root}")
    _validate_primary_worktree(root)
    codex_directory = root / ".codex"
    coordination = codex_directory / "coordination"
    _require_contained_directory(root, codex_directory, label=".codex directory")
    _require_contained_directory(root, coordination, label="coordination directory")
    marker = coordination / "project.yaml"
    if marker.exists():
        raise LifecycleError(
            "project marker already exists; use deactivate, migrate, or reactivate"
        )
    if coordination.exists() and any(coordination.iterdir()):
        raise LifecycleError(
            "coordination directory is not empty; preserve and reconcile it before init"
        )
    return root, coordination


def _initial_marker(
    *, project_id: str, project_name: str, task_prefix: str
) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", project_id):
        raise LifecycleError("--project-id must be a stable lowercase slug")
    project_name = project_name.strip()
    if not 1 <= len(project_name) <= 120 or any(
        character in "\r\n" or ord(character) < 32 for character in project_name
    ):
        raise LifecycleError("--project-name must be one plain-text line")
    if not TASK_PREFIX.fullmatch(task_prefix):
        raise LifecycleError(
            "--task-prefix must be 1 to 16 uppercase letters, digits, or hyphens"
        )
    return "\n".join(
        [
            "schema_version: 2",
            "coordination_enabled: true",
            f"project_id: {project_id}",
            f"project_name: {json.dumps(project_name, ensure_ascii=True)}",
            f"task_prefix: {task_prefix}",
            "canonical_paths:",
            "  active: .codex/coordination/active",
            "  archive: .codex/coordination/archive",
            "access:",
            "  cross_project_task_access: false",
            "  cross_project_state_changes: false",
            "",
        ]
    )


def _init_operation(args: argparse.Namespace) -> dict[str, object]:
    root, coordination = _initialization_root(args.project_root)
    marker_path = coordination / "project.yaml"
    marker_text = _initial_marker(
        project_id=args.project_id or "",
        project_name=args.project_name or "",
        task_prefix=args.task_prefix or "",
    )
    agents_path = root / "AGENTS.md"
    ignore_path = root / ".gitignore"
    agents = _read_document(agents_path, project_root=root, required=False)
    ignore = _read_document(ignore_path, project_root=root, required=False)
    agents_document, agents_text = _add_exact_block(
        agents,
        agents_path,
        DISCOVERY_BLOCK,
        "Coordinator discovery",
        project_root=root,
    )
    ignore_document, ignore_text = _add_exact_block(
        ignore,
        ignore_path,
        IGNORE_BLOCK,
        "Coordinator ignore",
        project_root=root,
    )
    marker_document = Document(
        path=marker_path,
        project_root=root,
        raw=b"",
        text="",
        newline="\n",
        bom=False,
        existed=False,
    )
    actions = [
        {"action": "create-enabled-schema-2-marker", "path": str(marker_path)},
        {"action": "create-empty-active-board", "path": str(coordination / "active")},
        {"action": "create-empty-cold-archive", "path": str(coordination / "archive")},
    ]
    changes = [(marker_document, marker_text)]
    if agents_text != agents_document.text:
        actions.append({"action": "add-discovery-block", "path": str(agents_path)})
        changes.append((agents_document, agents_text))
    if ignore_text != ignore_document.text:
        actions.append({"action": "add-ignore-block", "path": str(ignore_path)})
        changes.append((ignore_document, ignore_text))

    if args.apply:
        created_directories: list[Path] = []
        try:
            for path in (
                root / ".codex",
                coordination,
                coordination / "active",
                coordination / "archive",
            ):
                if not path.exists():
                    path.mkdir()
                    created_directories.append(path)
            _apply_documents(changes)
        except Exception:
            for path in reversed(created_directories):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise

    return {
        "status": "applied" if args.apply else "planned",
        "operation": "init",
        "projectId": args.project_id,
        "projectRoot": str(root),
        "actions": actions,
        "activeClaimsCreated": 0,
        "nativeTasksCreated": 0,
        "backgroundProcessesCreated": 0,
    }


def _apply_schema_one_migration(project: Project, marker_text: str) -> None:
    backup = project.coordination / MIGRATION_BACKUP_NAME
    if backup.exists():
        raise LifecycleError(f"schema-1 marker backup already exists: {backup}")
    board_paths = [project.coordination / "active", project.coordination / "archive"]
    created_directories: list[Path] = []
    backup_created = False
    marker_replacement: Document | None = None
    for path in board_paths:
        if _is_linklike(path):
            raise LifecycleError(f"schema-2 board path must not be linked: {path}")
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise LifecycleError(
                f"schema-2 board path must be absent or empty before migration: {path}"
            )
    try:
        with backup.open("xb") as stream:
            stream.write(project.marker.raw)
            stream.flush()
            os.fsync(stream.fileno())
        backup_created = True
        for path in board_paths:
            if not path.exists():
                path.mkdir()
                created_directories.append(path)
        if any(any(path.iterdir()) for path in board_paths):
            raise LifecycleError("schema-2 board changed during migration planning")
        marker_replacement = _atomic_write(
            project.marker, project.marker.encode(marker_text)
        )
    except Exception:
        if marker_replacement is not None:
            _atomic_write(marker_replacement, project.marker.raw)
        for path in reversed(created_directories):
            try:
                path.rmdir()
            except OSError:
                pass
        if backup_created:
            backup.unlink(missing_ok=True)
        raise


def _has_coordinator_discovery(document: Document | None) -> bool:
    if document is None:
        return False
    return any(
        _block_for(document, block) in document.text
        for block in (DISCOVERY_BLOCK, *LEGACY_DISCOVERY_BLOCKS)
    ) or DISCOVERY_HEADING in document.text


def _migration_operation(
    project: Project, args: argparse.Namespace, agents: Document | None
) -> dict[str, object]:
    if project.schema != 1:
        raise LifecycleError("only a schema-1 project can be migrated to schema 2")
    marker_text = _schema_two_marker(project)
    inventory = _legacy_inventory(project)
    native_actions = _native_actions(project, activate=False)
    blockers: list[str] = []
    if project.enabled:
        blockers.append("deactivate the schema-1 project first")
    if _has_coordinator_discovery(agents):
        blockers.append("remove the legacy Coordinator discovery block by running deactivate")
    if args.confirm_project_id != project.project_id:
        blockers.append("confirm the exact project ID with --confirm-project-id")
    if not args.confirm_legacy_runtime_stopped:
        blockers.append(
            "confirm the legacy Coordinator heartbeat and optional Mission Control are stopped"
        )
    backup = project.coordination / MIGRATION_BACKUP_NAME
    if backup.exists():
        blockers.append(f"remove or reconcile the existing marker backup: {backup}")
    for path in (project.coordination / "active", project.coordination / "archive"):
        if _is_linklike(path) or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            blockers.append(f"schema-2 board path must be absent or empty: {path}")

    actions = [
        {
            "action": "preserve-schema-1-marker",
            "path": str(project.coordination / MIGRATION_BACKUP_NAME),
        },
        {"action": "write-disabled-schema-2-marker", "path": str(project.marker.path)},
        {
            "action": "create-empty-active-board",
            "path": str(project.coordination / "active"),
        },
        {
            "action": "create-empty-cold-archive",
            "path": str(project.coordination / "archive"),
        },
    ]
    if args.apply:
        if blockers:
            raise LifecycleError("migration is not ready: " + "; ".join(blockers))
        _apply_schema_one_migration(project, marker_text)

    return {
        "status": "applied" if args.apply else "planned",
        "operation": "migrate",
        "projectId": project.project_id,
        "projectRoot": str(project.root),
        "sourceSchema": 1,
        "targetSchema": 2,
        "targetEnabled": False,
        "readyToApply": not blockers,
        "blockers": blockers,
        "actions": actions,
        "requiredNativeActions": native_actions,
        "legacyState": inventory,
        "optionalObserverState": {
            "status": (
                "user-confirmed-stopped"
                if args.confirm_legacy_runtime_stopped
                else "not-inspected"
            ),
            "reason": "migration never reads private Codex state or external observer state",
        },
        "activeClaimsCreated": 0,
        "historyPreserved": True,
    }


def _native_actions(project: Project, *, activate: bool) -> list[dict[str, str]]:
    if project.schema == 2:
        return []
    coordinator = _coordinator_id(project)
    if activate:
        raise LifecycleError(
            "legacy schema 1 cannot be reactivated; migrate the disabled marker to schema 2"
        )
    actions = [{"action": "remove-repository-heartbeat", "targetThreadId": coordinator or "verify-current-coordinator"}]
    if coordinator:
        actions.extend(
            [
                {"action": "archive-coordinator-at-safe-boundary", "targetThreadId": coordinator},
                {"action": "unpin-coordinator", "targetThreadId": coordinator},
            ]
        )
    return actions


def project_operation(args: argparse.Namespace) -> dict[str, object]:
    if args.action == "init":
        return _init_operation(args)
    purge = args.action == "purge"
    project = _load_project(args.project_root, allow_resumed_purge=purge)
    agents_path = project.root / "AGENTS.md"
    ignore_path = project.root / ".gitignore"
    agents = _read_document(
        agents_path, project_root=project.root, required=False
    )
    ignore = _read_document(
        ignore_path, project_root=project.root, required=False
    )
    if args.action == "migrate":
        return _migration_operation(project, args, agents)
    actions: list[dict[str, str]] = []
    changes: list[tuple[Document, str]] = []

    if args.action == "deactivate":
        if project.resumed_purge:
            raise LifecycleError("cannot deactivate a project while purge is incomplete")
        marker_text = _replace_marker_enabled(project.marker, False)
        if marker_text != project.marker.text:
            actions.append({"action": "set-marker-disabled", "path": str(project.marker.path)})
            changes.append((project.marker, marker_text))
        if agents is not None:
            agents_text = _remove_discovery_block(agents)
            if agents_text != agents.text:
                actions.append({"action": "remove-discovery-block", "path": str(agents.path)})
                changes.append((agents, agents_text))
        native = _native_actions(project, activate=False)

    elif args.action == "reactivate":
        if project.resumed_purge:
            raise LifecycleError("cannot reactivate a project while purge is incomplete")
        if project.schema != 2:
            raise LifecycleError(
                "legacy schema 1 cannot be reactivated; migrate the disabled marker to schema 2"
            )
        _validate_schema_two_reactivation(project.marker)
        marker_text = _replace_marker_enabled(project.marker, True)
        if marker_text != project.marker.text:
            actions.append({"action": "set-marker-enabled", "path": str(project.marker.path)})
            changes.append((project.marker, marker_text))
        agents_document, agents_text = _ensure_current_discovery(
            agents, agents_path, project_root=project.root
        )
        if agents_text != agents_document.text:
            actions.append({"action": "restore-discovery-block", "path": str(agents_path)})
            changes.append((agents_document, agents_text))
        native = _native_actions(project, activate=True)

    else:
        if args.confirm_project_id != project.project_id:
            raise LifecycleError("purge requires --confirm-project-id matching the validated marker")
        if agents is not None:
            agents_text = _remove_discovery_block(agents)
            if agents_text != agents.text:
                actions.append({"action": "remove-discovery-block", "path": str(agents.path)})
                changes.append((agents, agents_text))
        if ignore is not None:
            ignore_text = _remove_exact_block(ignore, IGNORE_BLOCK, "Coordinator ignore")
            if ignore_text != ignore.text:
                actions.append({"action": "remove-ignore-block", "path": str(ignore.path)})
                changes.append((ignore, ignore_text))
        actions.append({"action": "remove-coordination-state", "path": str(project.coordination)})
        native = _native_actions(project, activate=False)

    if args.apply:
        _apply_documents(changes)
        if purge:
            quarantine = project.root / ".codex" / QUARANTINE_NAME
            if not project.resumed_purge:
                os.replace(project.coordination, quarantine)
            shutil.rmtree(quarantine)

    return {
        "status": "applied" if args.apply else "planned",
        "operation": args.action,
        "projectId": project.project_id,
        "projectRoot": str(project.root),
        "actions": actions,
        "requiredNativeActions": native,
        "historyPreserved": not purge,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    project = subparsers.add_parser("project", help="plan or apply one project lifecycle operation")
    project.add_argument(
        "action", choices=("init", "deactivate", "migrate", "reactivate", "purge")
    )
    project.add_argument("--project-root", required=True)
    project.add_argument("--project-id", help="stable lowercase ID required for init")
    project.add_argument("--project-name", help="display name required for init")
    project.add_argument("--task-prefix", help="short uppercase prefix required for init")
    project.add_argument("--apply", action="store_true", help="apply the planned filesystem changes")
    project.add_argument(
        "--confirm-project-id",
        help="required for schema migration and destructive project purge",
    )
    project.add_argument(
        "--confirm-legacy-runtime-stopped",
        action="store_true",
        help="confirm old Coordinator heartbeat and optional Mission Control are stopped",
    )
    project.set_defaults(handler=project_operation)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (LifecycleError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
