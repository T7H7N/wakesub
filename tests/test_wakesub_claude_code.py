import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PUBLIC = Path(__file__).resolve().parents[1]
EXAMPLE = PUBLIC / "examples" / "claude-code"
sys.path.insert(0, str(PUBLIC))

from wakesub.__main__ import load_config
from wakesub.adapters import Delivery, DeliveryError


class ClaudeCodeHookTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(scratch.cleanup)
        self.project = Path(scratch.name) / "project with spaces"
        self.project.mkdir()
        self.hooks = json.loads((EXAMPLE / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    def hook(self, name, session_id, **fields):
        args = self.hooks[name][0]["hooks"][0]["args"]
        script_args = [arg.replace("${CLAUDE_PROJECT_DIR}", PUBLIC.as_posix()) for arg in args]
        return subprocess.run(
            [sys.executable, *script_args],
            input=json.dumps({"hook_event_name": name, "session_id": session_id, **fields}),
            capture_output=True, text=True, encoding="utf-8", timeout=10,
            cwd=self.project, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(self.project)),
        )

    def start(self, session_id="session-a"):
        result = self.hook("SessionStart", session_id, source="startup")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        signal, = map(Path, output["watchPaths"])
        return signal, output["additionalContext"]

    def change(self, signal, session_id="session-a", operation="change"):
        return self.hook("FileChanged", session_id, file_path=str(signal), event=operation)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", "-m", "wakesub", *args], cwd=self.project,
            env=dict(os.environ, PYTHONPATH=str(PUBLIC), PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    def test_session_start_registers_one_absolute_signal_per_session(self):
        first, context = self.start()
        second, _ = self.start("session-b")
        self.assertNotEqual(first.parent, second.parent)
        self.assertTrue(first.is_absolute())
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        config = first.parent / "wakesub.json"
        adapter, _, _ = load_config(config)
        self.assertTrue(adapter.signal)
        self.assertEqual(adapter.inbox.resolve(), first.parent)
        for value in (first.parent.name, str(config), str(first.parent / "store")):
            self.assertIn(value, context)
        group, = self.hooks["FileChanged"]
        self.assertNotIn("matcher", group)
        self.assertTrue(group["hooks"][0]["asyncRewake"])

    def test_session_start_keeps_existing_signal_and_config(self):
        signal, _ = self.start()
        config = signal.parent / "wakesub.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["interval"] = 3
        config.write_text(json.dumps(data), encoding="utf-8")
        os.utime(signal, (1000000000, 1000000000))
        before = signal.stat().st_mtime_ns
        again, _ = self.start()
        self.assertEqual(again, signal)
        self.assertEqual(signal.stat().st_mtime_ns, before)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")), data)

    def test_cli_delivers_through_file_adapter_and_hook_rewakes_once(self):
        signal, _ = self.start()
        store = ["--store", str(signal.parent / "store")]
        config = ["--config", str(signal.parent / "wakesub.json")]
        marker = self.project / "finished.marker"
        notice = "The worker's report is ready: résumé.md."
        added = self.cli(*store, "add", "--file", str(marker), "--target", signal.parent.name,
                         "--say", notice)
        self.assertEqual(added.returncode, 0, added.stderr)
        identifier = added.stdout.strip()
        pending = signal.parent / f"{identifier}.md"
        delivered = signal.parent / f"{identifier}.delivered"
        checked = self.cli(*store, *config, "run-once")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("fired=0", checked.stdout)
        self.assertFalse(pending.exists())
        os.utime(signal, (1000000000, 1000000000))
        before = signal.stat().st_mtime_ns
        marker.touch()
        checked = self.cli(*store, *config, "run-once")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("fired=1", checked.stdout)
        self.assertTrue(pending.is_file())
        self.assertGreater(signal.stat().st_mtime_ns, before)
        changed = self.change(signal)
        self.assertEqual(changed.returncode, 2, changed.stderr)
        self.assertEqual(changed.stdout, "")
        self.assertIn(notice, changed.stderr)
        self.assertFalse(pending.exists())
        self.assertIn(notice, delivered.read_text(encoding="utf-8"))
        repeated = self.change(signal)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (0, "", ""))

    def test_wrong_signal_or_deleted_signal_leaves_notice_unread(self):
        first, _ = self.start()
        second, _ = self.start("session-b")
        notice = first.parent / ("a" * 32 + ".md")
        notice.write_text("The report is present.", encoding="utf-8")
        for result in (self.change(second), self.change(first, "session-b"),
                       self.change(first, operation="unlink")):
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertTrue(notice.is_file())

    def test_hook_drains_pending_notices_and_leaves_other_files(self):
        signal, _ = self.start()
        notices = {"a" * 32: "The first notice is available.", "b" * 32: "The second notice is available."}
        for identifier, message in notices.items():
            (signal.parent / (identifier + ".md")).write_text(message, encoding="utf-8")
        unrelated = signal.parent / "notes.md"
        unrelated.write_text("Project notes.", encoding="utf-8")
        result = self.change(signal, operation="add")
        self.assertEqual(result.returncode, 2, result.stderr)
        for identifier, message in notices.items():
            self.assertIn(message, result.stderr)
            self.assertTrue((signal.parent / (identifier + ".delivered")).is_file())
        self.assertTrue(unrelated.is_file())

    def test_failed_signal_can_retry_pending_file_without_replaying_delivered_notice(self):
        signal, _ = self.start()
        adapter, _, _ = load_config(signal.parent / "wakesub.json")
        delivery = Delivery("a" * 32, signal.parent.name, "fired", "The report is present.")
        pending = signal.parent / (delivery.subscription_id + ".md")
        signal.unlink()
        signal.mkdir()
        with self.assertRaisesRegex(DeliveryError, "signal"):
            adapter.deliver(delivery)
        before = pending.stat().st_mtime_ns
        signal.rmdir()
        adapter.deliver(delivery)
        self.assertEqual(pending.stat().st_mtime_ns, before)
        self.assertEqual(self.change(signal).returncode, 2)
        os.utime(signal, (1000000000, 1000000000))
        before = signal.stat().st_mtime_ns
        adapter.deliver(delivery)
        self.assertFalse(pending.exists())
        self.assertEqual(signal.stat().st_mtime_ns, before)

    def test_invalid_signal_setting_is_rejected(self):
        config = self.project / "config.json"
        config.write_text('{"adapter": {"type": "file", "inbox": ".", "signal": "yes"}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "signal must be a boolean"):
            load_config(config)
