"""Process-safe bounded logging and external command capture."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, TextIO, cast

from concurrent_log_handler import ConcurrentRotatingFileHandler


DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 4
DEFAULT_LOG_LEVEL = logging.INFO
LOG_FILENAME = "visual-forge.log"
LOG_CHUNK_SIZE = 16 * 1024
TRUE_VALUES = {"1", "true", "yes", "on"}
SENSITIVE_TERMS = ("password", "token", "secret", "api-key", "api_key", "apikey")
URL_CREDENTIALS_PATTERN = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<user>[^\s/@:]+):(?P<password>[^\s/@]+)@", re.IGNORECASE)
JSON_SECRET_PATTERN = re.compile(
    r'(?P<prefix>"(?:password|token|secret|api[-_]?key)"\s*:\s*")(?P<value>[^"]*)(?P<suffix>")',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LogConfig:
    log_path: Path
    max_bytes: int
    backup_count: int
    level: int
    disabled: bool
    warnings: tuple[str, ...]


class UtcFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        if datefmt is not None:
            return timestamp.strftime(datefmt)
        return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LogSession:
    def __init__(
        self,
        *,
        run_id: str,
        command: str,
        config: LogConfig,
        logger: logging.Logger | None,
        setup_warning: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.command = command
        self.config = config
        self.logger = logger
        self.setup_warning = setup_warning

    @property
    def enabled(self) -> bool:
        return self.logger is not None

    @property
    def log_path(self) -> Path:
        return self.config.log_path

    def event(self, message: str, *, level: int = logging.INFO, stream: str = "event") -> None:
        if self.logger is None:
            return
        self.logger.log(level, message, extra=self._extra(stream))

    def output(self, stream: str, message: str) -> None:
        level = logging.ERROR if stream == "stderr" else logging.INFO
        chunks = _chunks(message, LOG_CHUNK_SIZE)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            suffix = f" [part {index}/{total}]" if total > 1 else ""
            self.event(f"{chunk}{suffix}", level=level, stream=stream)

    def exception(self, message: str) -> None:
        if self.logger is None:
            return
        self.logger.exception(message, extra=self._extra("exception"))

    def start(self, argv: list[str]) -> None:
        rendered = json.dumps(sanitize_argv(argv), ensure_ascii=True, separators=(",", ":"))
        self.event(f"command started argv={rendered}")
        for warning in self.config.warnings:
            self.event(warning, level=logging.WARNING)

    def finish(self, exit_code: int, elapsed_seconds: float) -> None:
        self.event(f"command finished exit_code={exit_code} elapsed_seconds={elapsed_seconds:.3f}")

    def close(self) -> None:
        if self.logger is None:
            return
        handlers = list(self.logger.handlers)
        for handler in handlers:
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)

    def _extra(self, stream: str) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "stream": stream,
        }


class CapturedTextStream:
    def __init__(self, original: TextIO, session: LogSession, stream_name: str, *, forward: bool) -> None:
        self._original = original
        self._session = session
        self._stream_name = stream_name
        self._forward = forward
        self._buffer = ""

    @property
    def encoding(self) -> str:
        return self._original.encoding or "utf-8"

    def write(self, text: str) -> int:
        if self._forward:
            self._original.write(text)
        self._buffer += text
        self._consume_complete_lines()
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._session.output(self._stream_name, self._buffer)
            self._buffer = ""
        if self._forward:
            self._original.flush()

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        return self._original.isatty()

    def _consume_complete_lines(self) -> None:
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._session.output(self._stream_name, line.rstrip("\r"))


def create_log_session(argv: list[str]) -> LogSession:
    config = load_log_config()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    command = extract_command(argv)
    if config.disabled:
        return LogSession(run_id=run_id, command=command, config=config, logger=None)

    try:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = ConcurrentRotatingFileHandler(
            config.log_path,
            mode="a",
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(
            UtcFormatter(
                "%(asctime)s %(levelname)s run=%(run_id)s pid=%(process)d "
                "command=%(command)s stream=%(stream)s %(message)s"
            )
        )
        logger = logging.getLogger(f"visual_forge.{os.getpid()}.{run_id}")
        logger.setLevel(config.level)
        logger.propagate = False
        logger.addHandler(handler)
        return LogSession(run_id=run_id, command=command, config=config, logger=logger)
    except Exception as exc:  # noqa: BLE001 - logging failure must not block the command.
        warning = f"Logging unavailable: {type(exc).__name__}: {exc}"
        return LogSession(run_id=run_id, command=command, config=config, logger=None, setup_warning=warning)


def load_log_config() -> LogConfig:
    warnings: list[str] = []
    repo_root = Path(__file__).resolve().parents[1]
    configured_dir = os.environ.get("VISUAL_FORGE_LOG_DIR")
    log_dir = Path(configured_dir) if configured_dir else repo_root / "logs"
    if not log_dir.is_absolute():
        log_dir = repo_root / log_dir

    max_bytes = _positive_int_env("VISUAL_FORGE_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES, warnings)
    backup_count = _positive_int_env("VISUAL_FORGE_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT, warnings)
    level = _log_level_env(warnings)
    disabled = os.environ.get("VISUAL_FORGE_LOG_DISABLED", "").strip().lower() in TRUE_VALUES
    return LogConfig(
        log_path=log_dir / LOG_FILENAME,
        max_bytes=max_bytes,
        backup_count=backup_count,
        level=level,
        disabled=disabled,
        warnings=tuple(warnings),
    )


@contextmanager
def capture_console(session: LogSession, *, forward: bool) -> Iterator[None]:
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    stdout_capture = CapturedTextStream(original_stdout, session, "stdout", forward=forward)
    stderr_capture = CapturedTextStream(original_stderr, session, "stderr", forward=forward)
    sys.stdout = cast(TextIO, stdout_capture)
    sys.stderr = cast(TextIO, stderr_capture)
    try:
        yield
    finally:
        stdout_capture.flush()
        stderr_capture.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr


def run_external_command(command: list[str], session: LogSession) -> int:
    normalized = command[1:] if command and command[0] == "--" else command
    if not normalized:
        print("error: run-logged requires a command after --", file=sys.stderr)
        return 2

    rendered = json.dumps(sanitize_argv(normalized), ensure_ascii=True, separators=(",", ":"))
    session.event(f"external command started argv={rendered}")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            normalized,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
    except OSError as exc:
        print(f"error: Could not start command: {exc}", file=sys.stderr)
        session.event(f"external command failed to start: {exc}", level=logging.ERROR)
        return 127

    if process.stdout is not None:
        for line in process.stdout:
            print(line, end="")
    exit_code = process.wait()
    elapsed = time.monotonic() - started
    session.event(f"external command finished exit_code={exit_code} elapsed_seconds={elapsed:.3f}")
    return exit_code


def sanitize_argv(argv: list[str]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            sanitized.append("***")
            redact_next = False
            continue

        if token.startswith("--") and "=" in token:
            name, _ = token.split("=", 1)
            if _is_sensitive_name(name):
                sanitized.append(f"{name}=***")
                continue

        sanitized_token = _redact_text(token)
        sanitized.append(sanitized_token)
        if token.startswith("--") and _is_sensitive_name(token):
            redact_next = True
    return sanitized


def extract_command(argv: list[str]) -> str:
    for token in argv:
        if token == "--":
            break
        if token == "--log-only" or token.startswith("-"):
            continue
        return token
    return "visual-forge"


def _redact_text(value: str) -> str:
    redacted = URL_CREDENTIALS_PATTERN.sub(r"\g<scheme>***:***@", value)
    return JSON_SECRET_PATTERN.sub(r"\g<prefix>***\g<suffix>", redacted)


def _is_sensitive_name(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return any(term.replace("_", "-") in normalized for term in SENSITIVE_TERMS)


def _chunks(value: str, size: int) -> list[str]:
    if not value:
        return [""]
    return [value[index : index + size] for index in range(0, len(value), size)]


def _positive_int_env(name: str, default: int, warnings: list[str]) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        warnings.append(f"Ignoring invalid {name}={value!r}; using {default}.")
        return default
    if parsed < 1:
        warnings.append(f"Ignoring invalid {name}={value!r}; using {default}.")
        return default
    return parsed


def _log_level_env(warnings: list[str]) -> int:
    value = os.environ.get("VISUAL_FORGE_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(value)
    if isinstance(level, int):
        return level
    warnings.append(f"Ignoring invalid VISUAL_FORGE_LOG_LEVEL={value!r}; using INFO.")
    return DEFAULT_LOG_LEVEL
