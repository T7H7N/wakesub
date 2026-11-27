import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PUBLIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PUBLIC))

from wakesub.__main__ import main
from wakesub.adapters import DeliveryUncertain, NoopAdapter
from wakesub.engine import run_once
from wakesub.store import Record, Store, write_json


DAY = 86400


class PruneTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(scratch.cleanup)
        self.store = Store(Path(scratch.name) / "store")
        self.store.root.mkdir()
        self.now = 1900000000
        self.next_id = 0

    def record(self, stage, age, *, predecessor=None, legacy=False):
        self.next_id += 1
        identifier = f"{self.next_id:032x}"
        path = self.store.root / (identifier + ".json")
        record = Record(
            identifier, "after" if predecessor else "at", predecessor or "1", "demo",
            "The condition was observed.", self.now - 60 * DAY, None, False, stage=stage,
            event=stage if stage in ("expired", "fired") else None,
            finished_at=None if stage == "open" else self.now - age,
        )
        self.store.save(record)
        if legacy:
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["finished_at"]
            write_json(path, data)
            os.utime(path, (self.now - age, self.now - age))
        return identifier

    def prune(self, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main(
                    ["--store", str(self.store.root), "prune", *options],
                    clock=lambda: self.now,
                )
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue().strip()

    def test_default_retention_cutoffs_and_recent_records(self):
        self.record("fired", 8 * DAY)
        self.record("expired", 9 * DAY)
        self.record("failed", 30 * DAY)
        recent = self.record("fired", 8 * DAY - 1)
        recent_failure = self.record("failed", 30 * DAY - 1)
        pending = self.record("open", 0)
        self.assertEqual(self.prune(), (0, "pruned=3"))
        self.assertEqual(set(self.store.load()), {recent, recent_failure, pending})
        self.assertEqual(self.prune(), (0, "pruned=0"))

    def test_overrides_can_prune_done_and_failed_at_different_ages(self):
        self.record("fired", 0)
        recently_failed = self.record("failed", 2 * DAY - 1)
        self.record("failed", 2 * DAY)
        self.assertEqual(self.prune("--done-days", "0", "--failed-days", "2"), (0, "pruned=2"))
        self.assertEqual(set(self.store.load()), {recently_failed})

    def test_legacy_records_use_file_mtime(self):
        self.record("expired", 9 * DAY, legacy=True)
        recent = self.record("fired", DAY, legacy=True)
        self.assertEqual(self.prune(), (0, "pruned=1"))
        self.assertEqual(set(self.store.load()), {recent})

    def test_open_dependency_keeps_predecessor_until_it_finishes(self):
        predecessor = self.record("fired", 60 * DAY)
        dependent = self.record("open", 0, predecessor=predecessor)
        self.assertEqual(self.prune(), (0, "pruned=0"))
        cycle = run_once(self.store, NoopAdapter(), clock=lambda: self.now)
        self.assertEqual(cycle.fired, 1)
        self.assertEqual(self.store.load()[dependent].finished_at, self.now)
        self.assertEqual(self.prune(), (0, "pruned=1"))
        self.assertEqual(set(self.store.load()), {dependent})

    def test_completion_timestamp_uses_clock_after_delivery(self):
        identifier = self.record("open", 0)

        class DelayedFailure:
            def deliver(adapter, delivery):
                self.now += 17
                raise DeliveryUncertain("The destination did not confirm receipt.")

        with self.assertLogs("wakesub", level="ERROR"):
            cycle = run_once(self.store, DelayedFailure(), clock=lambda: self.now)
        self.assertEqual(cycle.failed, 1)
        completed = self.store.load()[identifier]
        self.assertEqual(completed.finished_at, 1900000017)

    def test_invalid_retention_leaves_records_unchanged(self):
        identifier = self.record("fired", 90 * DAY)
        for flag, value in (("--done-days", "-1"), ("--failed-days", "1.5")):
            with self.subTest(flag=flag):
                self.assertEqual(self.prune(flag, value), (2, ""))
                self.assertEqual(set(self.store.load()), {identifier})
