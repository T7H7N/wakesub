"""Command-line entry point."""

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .adapters import PLACEHOLDER, CommandAdapter, FileAdapter, NoopAdapter
from .engine import run_once
from .store import RECORD_NAME, Record, Store, StoreBusy


def retention_days(value):
    days = int(value)
    if days < 0:
        raise argparse.ArgumentTypeError("retention days must be nonnegative")
    return days


def parser():
    cli = argparse.ArgumentParser(prog="wakesub", description="Poll and deliver wake subscriptions.")
    cli.add_argument("--store", type=Path, default=Path(".wakesub"), help="state directory")
    cli.add_argument("--config", type=Path, default=Path("wakesub.json"), help="delivery configuration")
    commands = cli.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="register one subscription")
    condition = add.add_mutually_exclusive_group(required=True)
    condition.add_argument("--at", help="a future local time or ISO date and time with an offset")
    condition.add_argument("--file", help="wait for a regular file")
    condition.add_argument("--file-nonempty", help="wait for a regular file with content")
    condition.add_argument("--after", help="wait for a subscription to reach fired")
    expiry = add.add_mutually_exclusive_group()
    expiry.add_argument("--ttl", help="positive duration in s, m, h or d")
    expiry.add_argument("--until", help="an explicit expiry date and time")
    add.add_argument("--quiet-expire", action="store_true", help="record expiry without delivery")
    add.add_argument("--say", required=True, help="wake message, up to 300 characters")
    add.add_argument("--target", required=True, help="destination interpreted by the adapter")
    commands.add_parser("list", help="show records, including terminal stages")
    drop = commands.add_parser("drop", help="remove a subscription record")
    drop.add_argument("id")
    prune = commands.add_parser("prune", help="remove old terminal records")
    prune.add_argument(
        "--done-days", type=retention_days, default=8,
        help="days to keep fired or expired records (default: 8)",
    )
    prune.add_argument(
        "--failed-days", type=retention_days, default=30,
        help="days to keep failed records (default: 30)",
    )
    commands.add_parser("run-once", help="run a single polling cycle")
    commands.add_parser("daemon", help="repeat polling at the configured interval")
    return cli


def future_time(value, now, option):
    try:
        if re.fullmatch(r"\d{2}:\d{2}", value):
            today = datetime.fromtimestamp(now).date()
            moment = datetime.combine(today, datetime.strptime(value, "%H:%M").time())
        elif re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:[0-5]\d)?", value
        ):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError
        result = moment.timestamp()
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError(f"{option} requires HH:MM or an ISO date and time") from exc
    if result <= now:
        raise ValueError(f"{option} must be in the future")
    return result


def duration(value):
    matched = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smhd])", value)
    if not matched:
        raise ValueError("--ttl requires a positive duration such as 6h")
    seconds = float(matched[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[matched[2]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("--ttl requires a finite positive duration")
    return seconds


def checked_text(value, option, limit):
    if not value.strip() or len(value) > limit or not value.isprintable():
        raise ValueError(f"{option} requires 1 to {limit} printable characters")
    return value


def subscription(args, now):
    if args.at is not None:
        kind, value = "at", str(future_time(args.at, now, "--at"))
    elif args.after is not None:
        kind, value = "after", args.after
    else:
        kind = "file" if args.file is not None else "file-nonempty"
        path = args.file if args.file is not None else args.file_nonempty
        if not path or "\0" in path:
            raise ValueError("a file condition requires a path")
        value = os.path.abspath(Path(path).expanduser())
    expires = None
    if args.until is not None:
        expires = future_time(args.until, now, "--until")
    elif args.ttl is not None or kind != "at":
        default_ttl = "2d" if kind == "after" else "6h"
        expires = now + duration(args.ttl if args.ttl is not None else default_ttl)
        if expires <= now:
            raise ValueError("--ttl is too small")
        try:
            datetime.fromtimestamp(expires, timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError("--ttl places expiry outside the supported date range") from exc
    if kind == "at" and expires is not None and expires <= float(value):
        raise ValueError("expiry must be later than --at")
    return Record(
        uuid4().hex, kind, value, checked_text(args.target, "--target", 200),
        checked_text(args.say, "--say", 300), now, expires, args.quiet_expire,
    )


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    allowed = {"adapter", "interval", "budget", "target_interval", "max_attempts"}
    if not isinstance(config, dict) or config.keys() - allowed:
        raise ValueError("invalid configuration fields")
    interval = config.get("interval", 10)
    budget = config.get("budget", 5)
    target_interval = config.get("target_interval", 30)
    max_attempts = config.get("max_attempts", 3)
    if type(budget) is not int or budget < 1 or type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("budget and max_attempts must be positive integers")
    if not valid_seconds(interval) or not valid_seconds(target_interval, allow_zero=True):
        raise ValueError("interval must be positive and target_interval must be nonnegative")
    spec = config.get("adapter", {})
    if not isinstance(spec, dict):
        raise ValueError("adapter must be an object")
    kind = spec.get("type", "command")
    if kind == "command" and not spec.keys() - {"type", "argv", "timeout"}:
        argv, timeout = spec.get("argv"), spec.get("timeout", 30)
        if (
            not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0].strip() or PLACEHOLDER.search(argv[0])
        ):
            raise ValueError("command adapter requires argv with a fixed executable and string arguments")
        if not valid_seconds(timeout):
            raise ValueError("command timeout must be positive")
        adapter = CommandAdapter(argv, timeout)
    elif kind == "file" and not spec.keys() - {"type", "inbox", "signal"}:
        inbox = spec.get("inbox")
        if not isinstance(inbox, str) or not inbox or "\0" in inbox:
            raise ValueError("file adapter requires an inbox path")
        signal = spec.get("signal", False)
        if not isinstance(signal, bool):
            raise ValueError("file adapter signal must be a boolean")
        adapter = FileAdapter(path.parent / Path(inbox).expanduser(), signal=signal)
    elif kind == "noop" and spec.keys() == {"type"}:
        adapter = NoopAdapter()
    else:
        raise ValueError("invalid adapter configuration")
    return adapter, interval, dict(budget=budget, target_interval=target_interval, max_attempts=max_attempts)


def valid_seconds(value, *, allow_zero=False):
    return type(value) in (int, float) and math.isfinite(value) and (
        value > 0 or (allow_zero and value == 0)
    )


def manage(args, store, cli, clock):
    record = None
    try:
        if args.command == "add":
            record = subscription(args, clock())
        if args.command == "drop" and not RECORD_NAME.fullmatch(args.id):
            raise ValueError("drop requires a subscription ID from list")
    except ValueError as exc:
        cli.error(str(exc))
    with store.locked():
        if args.command == "add":
            if record.kind == "after" and record.value not in store.load():
                cli.error("--after requires an existing subscription ID")
            store.save(record)
            print(record.id)
            return
        records = store.load()
        if args.command == "list":
            for item in sorted(records.values(), key=lambda entry: (entry.created_at, entry.id)):
                print(f"{item.id}\t{item.stage}\t{item.kind}\t{item.target}\t{item.say}")
        elif args.command == "prune":
            removed = store.prune(records, clock(), args.done_days, args.failed_days)
            print(f"pruned={removed}")
        else:
            if args.id not in records:
                cli.error("subscription ID was not found")
            if any(
                item.stage == "open" and item.kind == "after" and item.value == args.id
                for item in records.values()
            ):
                cli.error("an open subscription still depends on this ID")
            store.drop(records[args.id])


def main(argv=None, *, clock=time.time, sleep=time.sleep):
    cli = parser()
    args = cli.parse_args(argv)
    store = Store(args.store)
    logging.basicConfig(format="%(levelname)s %(message)s")
    try:
        if args.command in ("add", "list", "drop", "prune"):
            manage(args, store, cli, clock)
            return 0
        adapter, interval, limits = load_config(args.config)
        while True:
            try:
                cycle = run_once(store, adapter, clock=clock, **limits)
                print(
                    f"attempted={cycle.attempted} fired={cycle.fired} "
                    f"expired={cycle.expired} failed={cycle.failed}", flush=True
                )
            except StoreBusy as exc:
                if args.command == "run-once":
                    raise
                logging.warning("%s", exc)
            if args.command == "run-once":
                return 0
            sleep(interval)
    except KeyboardInterrupt:
        return 130
    except (OSError, OverflowError, ValueError) as exc:
        print(f"wakesub: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
