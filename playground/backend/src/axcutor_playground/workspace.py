"""Text-only workspace storage; no Python imports and no execution state.

One backend owns a data directory. Its async lock serializes whole-workspace edits
with Run preparation; a process lock prevents two backends sharing the volume.
Revisions hash ordered paths and exact UTF-8 contents, including generated files.
Only the backend writes this tree: external filesystem writers must be stopped.
"""

import asyncio
import fcntl
import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any, TextIO, TypedDict

from axcutor_host.service import HostError, InvalidRequest
from axcutor_runtime.packages import PackageMount, package_path


class WorkspaceConflict(HostError):
    code = "workspace_conflict"


class WorkspaceFile(TypedDict):
    path: str
    text: str
    generated: bool


class WorkspaceView(TypedDict):
    revision: str
    files: list[WorkspaceFile]


def source_path(value: Any) -> str:
    """Accept canonical relative source paths, never filesystem aliases."""
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise InvalidRequest("file path must be a workspace-relative source path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in (".", "..") for part in path.parts)
        or "__pycache__" in path.parts
        or path.suffix not in (".yaml", ".yml", ".json", ".py")
    ):
        raise InvalidRequest(f"invalid source path: {value!r}")
    return value


def generated(path: str) -> bool:
    return PurePosixPath(path).name.endswith("_contract.py")


def read_files(root: Path) -> list[WorkspaceFile]:
    """Reject symlinks/special files before reading, including directory links."""
    files: list[WorkspaceFile] = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                if name == "__pycache__":
                    dirs.remove(name)  # CLI imports may leave caches in local templates.
                continue
            if not stat.S_ISREG(mode):
                raise InvalidRequest(f"workspace requires ordinary files/directories: {path}")
            relative = source_path(path.relative_to(root).as_posix())
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeError as error:
                raise InvalidRequest(f"source must be UTF-8: {relative}") from error
            files.append({"path": relative, "text": text, "generated": generated(relative)})
    return sorted(files, key=lambda item: item["path"])


def write_files(root: Path, files: list[WorkspaceFile]) -> None:
    """Materialize a validated file set into a new, private directory."""
    root.mkdir(parents=True)
    for file in files:
        target = root / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file["text"], encoding="utf-8", newline="")


def discover_mounts(root: Path) -> list[PackageMount]:
    """Only public Python root packages under workflows/ become explicit mounts."""
    directory = root / "workflows"
    if not directory.is_dir():
        return []
    return [
        PackageMount(path.name, path)
        for path in sorted(directory.iterdir())
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and package_path(path.name, public=True)
        and "." not in path.name
    ]


class Workspace:
    """Persistent shared source files with optimistic whole-workspace saves.

    Replacements use a staged tree and a backup. Startup restores the backup if
    interrupted between the two renames; a completed replacement wins otherwise.
    Generated contracts must be returned unchanged, including on deletions.
    """

    def __init__(self, data: Path, examples: Path) -> None:
        self.data = data.resolve()
        self.examples = examples
        self.root = self.data / "workspace"
        self.lock = asyncio.Lock()
        self._owner: TextIO | None = None

    def open(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self._owner = (self.data / ".host.lock").open("a")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backup = self.data / ".workspace-backup"
            if backup.exists() and not self.root.exists():
                backup.rename(self.root)
            if backup.exists():
                shutil.rmtree(backup)
            stage = self.data / ".workspace-stage"
            if stage.exists():
                shutil.rmtree(stage)
            if not self.root.exists():
                if not self.examples.is_dir() or self.examples.is_symlink():
                    raise InvalidRequest("examples must be an ordinary template directory")
                write_files(stage, read_files(self.examples))
                stage.rename(self.root)
            if self.root.is_symlink() or not self.root.is_dir():
                raise InvalidRequest("workspace must be an ordinary directory")
            read_files(self.root)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._owner:
            self._owner.close()
            self._owner = None

    def view(self) -> WorkspaceView:
        files = read_files(self.root)
        digest = hashlib.sha256()
        for file in files:
            for value in (file["path"], file["text"]):
                data = value.encode("utf-8")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
        return {"revision": digest.hexdigest(), "files": files}

    def check_revision(self, revision: Any) -> WorkspaceView:
        if not isinstance(revision, str) or not revision:
            raise InvalidRequest("revision must be a nonempty string")
        current = self.view()
        if revision != current["revision"]:
            raise WorkspaceConflict("workspace changed; reload before saving or running")
        return current

    async def get(self) -> WorkspaceView:
        async with self.lock:
            return self.view()

    async def save(self, revision: Any, files: Any) -> WorkspaceView:
        async with self.lock:
            current = self.check_revision(revision)
            if not isinstance(files, list):
                raise InvalidRequest("files must be an array of {path, text} objects")
            proposed: dict[str, WorkspaceFile] = {}
            for file in files:
                if not isinstance(file, dict) or file.keys() != {"path", "text"}:
                    raise InvalidRequest("each file requires path and text")
                path = source_path(file["path"])
                if path in proposed or not isinstance(file["text"], str):
                    raise InvalidRequest("duplicate file path or non-text content")
                try:
                    file["text"].encode("utf-8")
                except UnicodeError as error:
                    raise InvalidRequest("source must be UTF-8") from error
                proposed[path] = {"path": path, "text": file["text"], "generated": generated(path)}
            for path in proposed:
                if any(parent.as_posix() in proposed for parent in PurePosixPath(path).parents):
                    raise InvalidRequest(f"file/directory collision: {path}")
            original_generated = {f["path"]: f for f in current["files"] if f["generated"]}
            if original_generated != {p: f for p, f in proposed.items() if f["generated"]}:
                raise InvalidRequest("generated contracts are read-only")
            stage, backup = self.data / ".workspace-stage", self.data / ".workspace-backup"
            try:
                write_files(stage, list(proposed.values()))
                self.root.rename(backup)
                try:
                    stage.rename(self.root)
                except OSError:
                    backup.rename(self.root)
                    raise
                shutil.rmtree(backup)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return self.view()
