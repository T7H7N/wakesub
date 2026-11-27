import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PUBLIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PUBLIC))

from wakesub.__main__ import load_config, main
from wakesub.adapters import CommandAdapter, Delivery, DeliveryError, DeliveryUncertain, FileAdapter
from wakesub.engine import run_once
from wakesub.store import Store, write_json


class Clock:
    def __init__(self):
        self.now = 1893456000.0  # Midnight UTC on the fixture date.

    def __call__(self):
        return self.now


class RecordingAdapter:
    def __init__(self, failure=None):
        self.deliveries = []
        self.failure = failure

    def deliver(self, delivery):
        self.deliveries.append(delivery)
        if self.failure is not None:
            raise self.failure


class WakeSubTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.store = Store(self.root / "store")
        self.clock = Clock()
        self.sink = RecordingAdapter()
        self.args = ["--store", str(self.store.root)]
        self.config = self.root / "settings.json"
        self.config.write_text('{"adapter": {"type": "noop"}}', encoding="utf-8")

    def cli(self, *args, **kwargs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(self.args + list(args), clock=self.clock, **kwargs)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue().strip(), stderr.getvalue().strip()

    def add(self, option, value, *options, target="demo"):
        code, output, error = self.cli(
            "add", option, str(value), "--target", target, "--say", "Review the finished report.", *options
        )
        self.assertEqual(code, 0, error)
        return output

    def poll(self, adapter=None, **options):
        options.setdefault("target_interval", 0)
        return run_once(self.store, adapter or self.sink, clock=self.clock, **options)

    def record(self, identifier):
        return Store(self.store.root).load()[identifier]

    def child(self, *args):
        env = dict(os.environ, PYTHONPATH=str(PUBLIC), PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-B", *args], cwd=self.root,
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=20
        )

    def test_timer_waits_until_exact_time(self):
        identifier = self.add("--at", "2030-01-01T00:01Z")
        self.clock.now += 59
        self.assertEqual(self.poll().attempted, 0)
        self.assertEqual(self.record(identifier).stage, "open")
        self.clock.now += 1
        self.assertEqual(self.poll().fired, 1)
        self.assertEqual(self.sink.deliveries[0].subscription_id, identifier)

    def test_file_waits_then_accepts_zero_bytes(self):
        path = self.root / "marker"
        identifier = self.add("--file", path)
        self.assertEqual(self.poll().fired, 0)
        path.mkdir()
        self.assertEqual(self.poll().fired, 0)
        path.rmdir()
        path.touch()
        self.assertEqual(self.poll().fired, 1)
        self.assertEqual(self.record(identifier).stage, "fired")

    def test_nonempty_waits_for_data(self):
        path = self.root / "report"
        identifier = self.add("--file-nonempty", path)
        self.assertEqual(self.poll().fired, 0)
        path.touch()
        self.assertEqual(self.poll().fired, 0)
        path.write_bytes(b"report")
        self.assertEqual(self.poll().fired, 1)
        self.assertEqual(self.record(identifier).stage, "fired")

    def test_existing_file_fires_on_poll_only(self):
        path = self.root / "ready"
        path.write_bytes(b"report")
        identifier = self.add("--file-nonempty", path)
        self.assertEqual(self.record(identifier).stage, "open")
        self.assertEqual(self.poll().fired, 1)

    def test_relative_watch_path_survives_working_directory_change(self):
        original = Path.cwd()
        try:
            os.chdir(self.root)
            identifier = self.add("--file", "ready")
        finally:
            os.chdir(original)
        (self.root / "ready").touch()
        self.assertEqual(self.poll().fired, 1)
        self.assertEqual(self.record(identifier).stage, "fired")

    def test_dependency_waits_for_fired(self):
        path = self.root / "marker"
        parent = self.add("--file", path)
        self.clock.now += 1
        dependent = self.add("--after", parent)
        self.assertEqual(self.poll().attempted, 0)
        self.assertEqual(self.record(dependent).stage, "open")
        path.touch()
        self.assertEqual(self.poll().fired, 2)
        self.assertEqual([item.subscription_id for item in self.sink.deliveries], [parent, dependent])

    def test_expired_parent_does_not_fire_child(self):
        parent = self.add("--file", self.root / "missing", "--ttl", "1s")
        dependent = self.add("--after", parent)
        self.clock.now += 1
        self.assertEqual(self.poll().expired, 1)
        self.assertEqual(self.record(dependent).stage, "open")
        self.assertEqual(len(self.sink.deliveries), 1)

    def test_ttl_delivers_expiry_once(self):
        path = self.root / "missing"
        identifier = self.add("--file", path, "--ttl", "2s")
        self.clock.now += 1
        self.assertEqual(self.poll().attempted, 0)
        self.clock.now += 1
        path.touch()  # The poll reached the deadline before observing the file.
        self.assertEqual(self.poll().expired, 1)
        self.assertEqual(self.poll().attempted, 0)
        message, = self.sink.deliveries
        self.assertEqual(message.event, "expired")
        self.assertIn("expired", message.message)
        self.assertIn("Review the finished report.", message.message)
        self.assertEqual(self.record(identifier).stage, "expired")

    def test_until_and_quiet_expiry(self):
        identifier = self.add(
            "--file-nonempty", self.root / "empty", "--until", "2030-01-01T09:00:03+09:00", "--quiet-expire"
        )
        self.clock.now += 3
        cycle = self.poll()
        self.assertEqual((cycle.expired, cycle.attempted), (1, 0))
        self.assertEqual(self.record(identifier).stage, "expired")
        self.assertEqual(self.sink.deliveries, [])

    def test_default_expiry(self):
        watch = self.add("--file", self.root / "missing")
        dependent = self.add("--after", watch)
        timer = self.add("--at", "2030-01-01T00:01Z")
        self.assertEqual(self.record(watch).expires_at, self.clock.now + 6 * 3600)
        self.assertEqual(self.record(dependent).expires_at, self.clock.now + 2 * 86400)
        self.assertIsNone(self.record(timer).expires_at)

    def test_budget_keeps_observed_event_and_quiet_expiry(self):
        path = self.root / "marker"
        path.touch()
        first = self.add("--file", path)
        self.clock.now += 1
        second = self.add("--file", path, "--ttl", "2s")
        quiet = self.add("--file", self.root / "missing", "--ttl", "1s", "--quiet-expire")
        self.clock.now += 1
        cycle = self.poll(budget=1)
        self.assertEqual((cycle.attempted, cycle.fired, cycle.expired), (1, 1, 1))
        self.assertEqual(self.record(first).stage, "fired")
        self.assertEqual(self.record(quiet).stage, "expired")
        self.assertEqual(self.record(second).stage, "open")
        path.unlink()
        self.clock.now += 1
        self.assertEqual(self.poll().fired, 1)
        self.assertEqual(self.record(second).stage, "fired")

    def test_target_throttle_persists_and_does_not_block_other_targets(self):
        path = self.root / "marker"
        path.touch()
        first = self.add("--file", path)
        self.clock.now += 1
        later = self.add("--file", path)
        other = self.add("--file", path, target="other")
        self.assertEqual(self.poll(target_interval=5).fired, 2)
        self.assertEqual(self.record(first).stage, "fired")
        self.assertEqual(self.record(other).stage, "fired")
        self.assertEqual(self.cli("drop", first)[0], 0)
        self.store = Store(self.store.root)
        self.clock.now += 4
        self.assertEqual(self.poll(target_interval=5).attempted, 0)
        self.clock.now += 1
        self.assertEqual(self.poll(target_interval=5).fired, 1)
        self.assertEqual(self.record(later).stage, "fired")

    def test_delivery_retries_until_failure_and_logs_single_line(self):
        identifier = self.add("--file", self.config)
        receiver = RecordingAdapter(DeliveryError("unavailable\nreceiver"))
        self.assertEqual(self.poll(receiver, max_attempts=2).failed, 0)
        self.assertEqual(self.record(identifier).attempts, 1)
        self.assertEqual(self.record(identifier).stage, "open")
        with self.assertLogs("wakesub", "ERROR") as logs:
            self.assertEqual(self.poll(receiver, max_attempts=2).failed, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("unavailable receiver", logs.output[0])
        self.assertEqual(self.record(identifier).stage, "failed")
        self.assertEqual(self.poll(receiver).attempted, 0)
        self.assertEqual(len(receiver.deliveries), 2)

    def test_retry_uses_same_id_after_recovery(self):
        identifier = self.add("--file", self.config)
        receiver = RecordingAdapter(DeliveryError("unavailable"))
        self.assertEqual(self.poll(receiver).attempted, 1)
        receiver.failure = None
        self.assertEqual(self.poll(receiver).fired, 1)
        self.assertEqual([item.subscription_id for item in receiver.deliveries], [identifier, identifier])

    def test_expiry_delivery_failure_preserves_event(self):
        identifier = self.add("--file", self.root / "missing", "--ttl", "1s")
        self.clock.now += 1
        with self.assertLogs("wakesub", "ERROR"):
            self.poll(RecordingAdapter(DeliveryError("unavailable")), max_attempts=1)
        record = self.record(identifier)
        self.assertEqual((record.stage, record.event), ("failed", "expired"))

    def test_uncertain_delivery_is_never_retried(self):
        identifier = self.add("--file", self.config)
        reader = RecordingAdapter(DeliveryUncertain("possibly delivered"))
        with self.assertLogs("wakesub", "ERROR"):
            self.assertEqual(self.poll(reader).failed, 1)
        self.assertEqual(self.poll(reader).attempted, 0)
        self.assertEqual(self.record(identifier).attempts, 1)

    def test_crashed_process_releases_lock_without_replaying_delivery(self):
        identifier = self.add("--file", self.config)
        program = """
import os
import sys
from pathlib import Path
from wakesub.engine import run_once
from wakesub.store import Store
class CrashAfterHandoff:
    def deliver(self, delivery):
        Path('accepted').write_text(delivery.subscription_id, encoding='utf-8')
        os._exit(9)
run_once(Store(Path(sys.argv[1])), CrashAfterHandoff(), clock=lambda: float(sys.argv[2]))
"""
        result = self.child("-c", program, str(self.store.root), str(self.clock.now))
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual((self.root / "accepted").read_text(encoding="utf-8"), identifier)
        self.assertTrue(self.record(identifier).in_flight)
        with self.assertLogs("wakesub", "ERROR"):
            self.assertEqual(self.poll().failed, 1)
        self.assertEqual(self.sink.deliveries, [])
        self.assertEqual(self.record(identifier).stage, "failed")

    def test_overlapping_process_cannot_deliver_same_subscription(self):
        identifier = self.add("--file", self.config)
        case = self

        class CompetingAdapter(RecordingAdapter):
            def deliver(self, delivery):
                super().deliver(delivery)
                result = case.child("-m", "wakesub", *case.args, "--config", str(case.config), "run-once")
                case.assertEqual(result.returncode, 1, result.stdout)
                case.assertIn("busy", result.stderr)

        receiver = CompetingAdapter()
        self.assertEqual(self.poll(receiver).fired, 1)
        self.assertEqual(self.poll(receiver).attempted, 0)
        self.assertEqual([item.subscription_id for item in receiver.deliveries], [identifier])

    def test_atomic_replace_publishes_only_complete_json(self):
        path = self.root / "record.json"
        write_json(path, {"attempts": 0})
        original_replace = os.replace

        def observe(temporary, destination):
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"attempts": 0})
            self.assertEqual(json.loads(Path(temporary).read_text(encoding="utf-8")), {"attempts": 1})
            original_replace(temporary, destination)

        with patch("wakesub.store.os.replace", side_effect=observe) as replace:
            write_json(path, {"attempts": 1})
        replace.assert_called_once()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"attempts": 1})

    def test_replace_failure_preserves_state_and_cleans_temp_file(self):
        path = self.root / "record.json"
        write_json(path, {"attempts": 0})
        with patch("wakesub.store.os.replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                write_json(path, {"attempts": 1})
        self.assertEqual({entry.name for entry in self.root.iterdir()}, {"record.json", "settings.json"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"attempts": 0})

    def test_atomic_replace_retries_brief_windows_file_contention(self):
        path = self.root / "record.json"
        write_json(path, {"attempts": 0})
        original_replace = os.replace
        unavailable = PermissionError("fixture sharing violation")
        unavailable.winerror = 32
        calls = []

        def replace(source, destination):
            calls.append(source)
            if len(calls) == 1:
                raise unavailable
            original_replace(source, destination)

        with patch("wakesub.store.os.replace", side_effect=replace), patch("wakesub.store.time.sleep") as pause:
            write_json(path, {"attempts": 1})
        pause.assert_called_once_with(0.01)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"attempts": 1})

    def test_file_adapter_delivers_one_markdown_file(self):
        identifier = self.add("--file", self.config)
        inbox = self.root / "receiver"
        adapter = FileAdapter(inbox)
        self.assertEqual(self.poll(adapter).fired, 1)
        self.assertEqual(self.poll(adapter).attempted, 0)
        self.assertEqual([path.name for path in inbox.iterdir()], [identifier + ".md"])
        self.assertEqual((inbox / (identifier + ".md")).read_text(encoding="utf-8"),
                         "Target: demo\n\nReview the finished report.\n")

    def test_command_adapter_passes_utf8_to_stdin(self):
        output = self.root / "stdin"
        program = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        adapter = CommandAdapter([sys.executable, "-B", "-c", program, str(output)])
        adapter.deliver(Delivery("synthetic", "demo", "fired", "Wake 🌱"))
        self.assertEqual(output.read_bytes(), "Wake 🌱".encode("utf-8"))

    def test_command_adapter_passes_argument_as_data_and_expands_once(self):
        output = self.root / "args"
        program = "from pathlib import Path; import sys,json; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')"
        adapter = CommandAdapter([
            sys.executable, "-B", "-c", program, str(output), "{target}", "{message}", "{id}", "{event}"
        ])
        message = 'Wake \u2603 & | "quoted" {target}'
        adapter.deliver(Delivery("synthetic", "demo", "fired", message))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), ["demo", message, "synthetic", "fired"])

    def test_command_nonzero_can_retry_and_timeout_is_uncertain(self):
        adapter = CommandAdapter([sys.executable, "-B", "-c", "import sys; sys.exit(7)"])
        delivery = Delivery("synthetic", "demo", "fired", "Wake")
        with self.assertRaisesRegex(DeliveryError, "status 7"):
            adapter.deliver(delivery)
        with patch("wakesub.adapters.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            with self.assertRaisesRegex(DeliveryUncertain, "timed out"):
                adapter.deliver(delivery)

    def test_cli_validates_arguments(self):
        flags = ("--target", "demo", "--say", "Wake")
        cases = [
            ("add", *flags),
            ("add", "--at", "2030-01-01T00:00Z", *flags),
            ("add", "--at", "2030-01-01T00:01Z", "--ttl", "1s", *flags),
            ("add", "--file", "marker", "--after", "missing", *flags),
            ("add", "--file", "marker", "--ttl", "1s", "--until", "2030-01-02T00:00Z", *flags),
            ("add", "--file", "marker", "--ttl", "0s", *flags),
            ("add", "--file", "marker", "--ttl", "9" * 300 + "d", *flags),
            ("add", "--file", "marker", "--target", "demo", "--say", "w" * 301),
            ("add", "--file", "marker", "--target", "demo", "--say", "line\nline"),
            ("add", "--file", "marker", "--say", "Wake"),
            ("add", "--after", "missing", *flags),
            ("drop", "../not-a-record"),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, error = self.cli(*args)
                self.assertEqual(code, 2)
                self.assertIn("error:", error)
        self.assertEqual(self.store.load(), {})

    def test_list_and_drop_respect_open_dependencies(self):
        parent = self.add("--file", self.root / "missing")
        child = self.add("--after", parent)
        code, listing, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn(parent + "\topen", listing)
        self.assertIn(child + "\topen", listing)
        self.assertEqual(self.cli("drop", parent)[0], 2)
        self.assertEqual(self.cli("drop", child)[0], 0)
        self.assertEqual(self.cli("drop", parent)[0], 0)
        self.assertEqual(self.cli("list")[1], "")

    def test_cli_daemon_uses_same_cycle_with_injected_wait(self):
        path = self.root / "marker"
        identifier = self.add("--file", path)
        pauses = []

        def advance(delay):
            pauses.append(delay)
            if len(pauses) == 2:
                raise KeyboardInterrupt
            path.touch()
            self.clock.now += delay

        code, output, _ = self.cli("--config", str(self.config), "daemon", sleep=advance)
        self.assertEqual(code, 130)
        self.assertEqual(pauses, [10, 10])
        self.assertIn("attempted=0", output.splitlines()[0])
        self.assertIn("fired=1", output.splitlines()[1])
        self.assertEqual(self.record(identifier).stage, "fired")

    def test_config_file_inbox_is_relative_to_config_directory(self):
        self.config.write_text('{"adapter": {"type": "file", "inbox": "messages"}}', encoding="utf-8")
        identifier = self.add("--file", self.config)
        code, output, error = self.cli("--config", str(self.config), "run-once")
        self.assertEqual(code, 0, error)
        self.assertIn("fired=1", output)
        self.assertTrue((self.root / "messages" / (identifier + ".md")).is_file())

    def test_invalid_config_fails_before_advancing_records(self):
        identifier = self.add("--file", self.config)
        self.config.write_text('{"adapter": {"type": "noop"}, "budget": false}', encoding="utf-8")
        code, _, error = self.cli("--config", str(self.config), "run-once")
        self.assertEqual(code, 1)
        self.assertIn("positive integers", error)
        self.assertEqual(self.record(identifier).stage, "open")

    def test_invalid_record_stops_cycle_before_delivery(self):
        valid = self.add("--file", self.config)
        invalid = self.add("--file", self.config)
        path = self.store.root / (invalid + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["event"] = "unknown"
        write_json(path, record)
        code, _, error = self.cli("--config", str(self.config), "run-once")
        self.assertEqual(code, 1)
        self.assertIn("invalid subscription record", error)
        still_open = json.loads((self.store.root / (valid + ".json")).read_text(encoding="utf-8"))
        self.assertEqual((still_open["stage"], still_open["attempts"]), ("open", 0))

    def test_example_configs_load(self):
        command, _, _ = load_config(PUBLIC / "examples" / "command.json")
        receiver, _, _ = load_config(PUBLIC / "examples" / "file.json")
        self.assertIsInstance(command, CommandAdapter)
        self.assertIsInstance(receiver, FileAdapter)


if __name__ == "__main__":
    unittest.main()
