"""Local subscription records and process locking."""

import errno
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


RECORD_NAME = re.compile(r"[0-9a-f]{32}")


class StoreBusy(OSError):
    """Another process owns the store lock."""


@dataclass
class Record:
    id: str
    kind: str
    value: str
    target: str
    say: str
    created_at: float
    expires_at: Optional[float]
    quiet_expire: bool
    stage: str = "open"
    event: Optional[str] = None
    attempts: int = 0
    in_flight: bool = False
    last_error: Optional[str] = None
    finished_at: Optional[float] = None


def atomic_write(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def replace_file(source, destination):
    # On Windows a brief external open can block replacement of a closed file.
    pauses = iter((0.01, 0.03, 0.10))
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            delay = next(pauses, None)
            if delay is None or getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            time.sleep(delay)


def write_json(path: Path, value) -> None:
    data = json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False)
    atomic_write(path, (data + "\n").encode("utf-8"))


class Store:
    def __init__(self, root: Path):
        self.root = root

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # Keep this file in place. Replacing it would split the lock across processes.
        with (self.root / ".lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                try:
                    # The byte range can extend past the end of an empty file.
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise StoreBusy("subscription store is busy") from exc
                    raise
            else:
                import fcntl

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise StoreBusy("subscription store is busy") from exc
            # Closing the handle releases the lock on both platforms.
            yield

    def load(self):
        records = {}
        for path in self.root.iterdir():
            if path.suffix != ".json" or not RECORD_NAME.fullmatch(path.stem):
                continue
            try:
                record = Record(**json.loads(path.read_text(encoding="utf-8")))
                if (
                    record.id != path.stem
                    or record.stage not in ("open", "fired", "expired", "failed")
                    or record.kind not in ("at", "file", "file-nonempty", "after")
                    or record.event not in (None, "fired", "expired")
                    or (record.in_flight and (record.stage != "open" or record.event is None))
                    or (record.stage in ("fired", "expired") and record.event != record.stage)
                    or type(record.attempts) is not int or record.attempts < 0
                    or not all(isinstance(value, str) for value in (record.value, record.target, record.say))
                    or not is_timestamp(record.created_at)
                    or (record.expires_at is not None and not is_timestamp(record.expires_at))
                    or (record.finished_at is not None and (
                        record.stage == "open" or not is_timestamp(record.finished_at)
                    ))
                    or (record.kind == "at" and not math.isfinite(float(record.value)))
                ):
                    raise ValueError("invalid record fields")
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid subscription record {path.name}") from exc
            records[record.id] = record
        return records

    def save(self, record: Record) -> None:
        write_json(self.root / (record.id + ".json"), asdict(record))

    def drop(self, record: Record) -> None:
        (self.root / (record.id + ".json")).unlink()

    def prune(self, records, now, done_days, failed_days):
        """Remove old terminal records while the caller holds the store lock."""
        protected = {
            record.value for record in records.values()
            if record.stage == "open" and record.kind == "after"
        }
        removed = 0
        for record in records.values():
            if record.stage == "open" or record.id in protected:
                continue
            finished_at = record.finished_at
            if finished_at is None:
                # Older versions last wrote the record when it became terminal.
                finished_at = (self.root / (record.id + ".json")).stat().st_mtime
            days = failed_days if record.stage == "failed" else done_days
            if (now - finished_at) / 86400 >= days:
                self.drop(record)
                removed += 1
        return removed

    def load_attempt_times(self):
        try:
            times = json.loads((self.root / ".attempt-times.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(times, dict) or not all(is_timestamp(value) for value in times.values()):
            raise ValueError("invalid target interval data")
        return times

    def save_attempt_times(self, times) -> None:
        write_json(self.root / ".attempt-times.json", times)


def is_timestamp(value):
    return type(value) in (int, float) and math.isfinite(value)
