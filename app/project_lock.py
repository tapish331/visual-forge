"""Cross-process locks for mutating project commands."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import TextIO, cast

import portalocker

from .project import JsonObject


PROJECT_LOCK_FILENAME = ".visual-forge.lock"
PROJECT_LOCK_OWNER_SUFFIX = ".owner"


@dataclass(frozen=True)
class ProjectLockOwner:
    pid: int | None
    run_id: str | None
    command: str | None
    started_at: str | None


class ProjectBusyError(Exception):
    def __init__(self, project_dir: Path, owner: ProjectLockOwner | None) -> None:
        super().__init__(f"Project is busy: {project_dir}")
        self.project_dir = project_dir
        self.owner = owner


class ProjectMutationLock:
    def __init__(self, project_dir: Path, *, run_id: str, command: str) -> None:
        self.project_dir = project_dir
        self.run_id = run_id
        self.command = command
        self.lock_path = project_dir / PROJECT_LOCK_FILENAME
        self.owner_path = project_dir / f"{PROJECT_LOCK_FILENAME}{PROJECT_LOCK_OWNER_SUFFIX}"
        self._lock = portalocker.Lock(
            self.lock_path,
            mode="a+",
            timeout=0,
            fail_when_locked=True,
            encoding="utf-8",
        )
        self._handle: TextIO | None = None

    def __enter__(self) -> ProjectMutationLock:
        try:
            handle: TextIO = cast(TextIO, self._lock.acquire())
        except portalocker.exceptions.LockException as exc:
            raise ProjectBusyError(self.project_dir, read_lock_owner(self.lock_path)) from exc

        self._handle = handle
        metadata: JsonObject = {
            "pid": os.getpid(),
            "run_id": self.run_id,
            "command": self.command,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        self._handle.seek(0)
        self._handle.truncate()
        json.dump(metadata, self._handle, separators=(",", ":"))
        self._handle.write("\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        try:
            self.owner_path.write_text(json.dumps(metadata, separators=(",", ":")) + "\n", encoding="utf-8")
        except OSError:
            try:
                self.owner_path.unlink(missing_ok=True)
            except OSError:
                pass
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc_value, traceback
        self._lock.release()
        self._handle = None


def read_lock_owner(lock_path: Path) -> ProjectLockOwner | None:
    owner_path = lock_path.with_name(f"{lock_path.name}{PROJECT_LOCK_OWNER_SUFFIX}")
    owner = _read_owner_file(owner_path)
    if owner is not None:
        return owner
    return _read_owner_file(lock_path)


def _read_owner_file(path: Path) -> ProjectLockOwner | None:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    pid_value = data.get("pid")
    return ProjectLockOwner(
        pid=pid_value if isinstance(pid_value, int) and not isinstance(pid_value, bool) else None,
        run_id=_optional_string(data.get("run_id")),
        command=_optional_string(data.get("command")),
        started_at=_optional_string(data.get("started_at")),
    )


def format_project_busy_error(error: ProjectBusyError) -> str:
    lines = [f"error: Project is busy: {error.project_dir}"]
    owner = error.owner
    if owner is not None:
        details: list[str] = []
        if owner.pid is not None:
            details.append(f"PID {owner.pid}")
        if owner.command is not None:
            details.append(f"command {owner.command}")
        if owner.started_at is not None:
            details.append(f"started {owner.started_at}")
        if details:
            lines.append("Owner: " + ", ".join(details))
    return "\n".join(lines)


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
