"""Local delivery adapters. Import this module to implement your own adapter."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .store import atomic_write


class DeliveryError(Exception):
    """Delivery failed and the adapter permits another attempt."""


class DeliveryUncertain(Exception):
    """Delivery may have happened. Repeating it could send a duplicate."""


@dataclass(frozen=True)
class Delivery:
    subscription_id: str
    target: str
    event: str
    message: str


class Adapter(Protocol):
    def deliver(self, delivery: Delivery) -> None:
        """Return when the destination has accepted the message."""


PLACEHOLDER = re.compile(r"\{(target|message|id|event)\}")


@dataclass
class CommandAdapter:
    argv: list
    timeout: float = 30

    def deliver(self, delivery: Delivery) -> None:
        values = {
            "target": delivery.target,
            "message": delivery.message,
            "id": delivery.subscription_id,
            "event": delivery.event,
        }
        program, *arguments = self.argv
        program = shutil.which(program)
        if program is None:
            raise DeliveryError("command executable was not found")
        if os.name == "nt" and Path(program).suffix.lower() not in (".exe", ".com"):
            raise DeliveryError("command must be a native executable on Windows")
        argv = [program] + [PLACEHOLDER.sub(lambda match: values[match[1]], arg) for arg in arguments]
        in_arguments = any("{message}" in arg for arg in arguments)
        try:
            result = subprocess.run(
                argv,
                input=None if in_arguments else delivery.message.encode("utf-8"),
                stdin=subprocess.DEVNULL if in_arguments else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeliveryUncertain("command timed out") from exc
        except OSError as exc:
            raise DeliveryError("command could not be started") from exc
        if result.returncode:
            raise DeliveryError(f"command exited with status {result.returncode}")


@dataclass
class FileAdapter:
    inbox: Path
    signal: bool = False

    def deliver(self, delivery: Delivery) -> None:
        data = f"Target: {delivery.target}\n\n{delivery.message}\n".encode("utf-8")
        path = self.inbox / (delivery.subscription_id + ".md")
        candidates = (path, path.with_suffix(".delivered")) if self.signal else (path,)
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            for candidate in candidates:
                try:
                    present = candidate.read_bytes()
                except FileNotFoundError:
                    continue
                if present != data:
                    raise DeliveryUncertain("inbox already contains different content for this ID")
                if candidate != path:
                    return
                break
            else:
                atomic_write(path, data)
        except OSError as exc:
            raise DeliveryError("inbox file could not be written") from exc
        if self.signal:
            signal = self.inbox / "signal"
            try:
                if signal.is_dir():
                    raise IsADirectoryError(signal)
                signal.touch()
            except OSError as exc:
                raise DeliveryError("inbox signal could not be touched") from exc


class NoopAdapter:
    def deliver(self, delivery: Delivery) -> None:
        pass
