import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.zap2xml import plugin


CLI = Path(__file__).resolve().parents[1] / "src/zap2xml/zap2xml.py"


class CliLockTests(unittest.TestCase):
    def test_contention_and_sigkill_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            lock_dir = data / "plugins"
            legacy = lock_dir / "zap2xml.lock.d"
            legacy.mkdir(parents=True)
            env = {**os.environ, "DISPATCHARR_DATA_DIR": directory}
            holder_code = """
import runpy, sys, time
guard = runpy.run_path(sys.argv[1])["_single_instance_guard"]
with guard():
    print("locked", flush=True)
    time.sleep(30)
"""
            with subprocess.Popen(
                [sys.executable, "-B", "-c", holder_code, str(CLI)],
                env=env, stdout=subprocess.PIPE, text=True,
            ) as holder:
                try:
                    self.assertEqual(holder.stdout.readline().strip(), "locked")
                    self.assertFalse(legacy.exists())
                    inode = (lock_dir / "zap2xml.run.lock").stat().st_ino
                    blocked = subprocess.run(
                        [sys.executable, "-B", str(CLI), "--help"],
                        env=env, capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(blocked.returncode, plugin.LOCK_BUSY_EXIT)
                    self.assertIn("another instance is running", blocked.stderr)
                    holder.kill()
                    self.assertEqual(holder.wait(timeout=5), -signal.SIGKILL)
                finally:
                    if holder.poll() is None:
                        holder.kill()
            next_run = subprocess.run(
                [sys.executable, "-B", str(CLI), "--help"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(next_run.returncode, 0, next_run.stderr)
            self.assertEqual((lock_dir / "zap2xml.run.lock").stat().st_ino, inode)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.plugin_dir = Path(self.directory)
        self.state_path = self.plugin_dir / ".download-state.json"
        self.enterContext(patch.object(plugin, "PLUGIN_DIR", self.plugin_dir))
        self.enterContext(patch.object(plugin, "_ensure_scheduler_started"))
        self.now = datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc)
        self.clock = self.enterContext(patch.object(plugin, "datetime", wraps=datetime))
        self.clock.now.side_effect = lambda _tz: self.now
        self.settings = {"lineup_ids": "USA-OTA34102", "refresh_interval_hours": 24}
        self.config = SimpleNamespace(enabled=True, settings=self.settings)
        config_model = SimpleNamespace(objects=Mock())
        config_model.objects.filter.return_value.first.return_value = self.config
        self.enterContext(patch.dict(sys.modules, {
            "apps.plugins.models": SimpleNamespace(PluginConfig=config_model),
        }))
        self.logger = Mock(spec=logging.Logger)
        self.instance = plugin.Plugin()

    def test_failed_or_skipped_attempt_backs_off_every_worker(self):
        for status in ("error", "skipped"):
            with self.subTest(status=status):
                self.logger.reset_mock()
                self.state_path.unlink(missing_ok=True)
                started = self.now

                def download(_instance, _action, _params, _context):
                    state = json.loads(self.state_path.read_text())
                    self.assertEqual(state["last_attempt"], self.now.isoformat())
                    self.assertNotIn("last_generated", state)
                    return {"status": status, "message": "fixture result"}

                with patch.object(plugin.Plugin, "_run_action", autospec=True, side_effect=download) as action:
                    plugin._scheduler_tick(self.logger)
                    self.now = started + timedelta(minutes=29, seconds=59)
                    plugin._scheduler_tick(self.logger)
                    self.assertEqual(action.call_count, 1)
                    self.assertEqual(self.logger.warning.call_count, int(status == "error"))
                    self.assertEqual(self.logger.info.call_count, 1 + int(status == "skipped"))
                    self.now = started + timedelta(minutes=30)
                    plugin._scheduler_tick(self.logger)
                    self.assertEqual(action.call_count, 2)

    def test_success_uses_completion_time_and_24_hour_interval(self):
        def completed(_instance, _action, _params, _context):
            self.now += timedelta(minutes=4)
            return {"status": "queued"}

        with patch.object(plugin.Plugin, "_run_action", autospec=True, side_effect=completed) as action:
            plugin._scheduler_tick(self.logger)
            finished = self.now
            self.assertEqual(json.loads(self.state_path.read_text())["last_generated"], finished.isoformat())
            self.now = finished + timedelta(hours=23, minutes=59)
            plugin._scheduler_tick(self.logger)
            self.assertEqual(action.call_count, 1)
            self.now = finished + timedelta(hours=24)
            plugin._scheduler_tick(self.logger)
            self.assertEqual(action.call_count, 2)

    def test_restart_honors_legacy_success_and_disabled_schedule(self):
        self.settings["last_generated"] = "2026-11-24T00:00:00Z"
        with patch.object(plugin.Plugin, "_run_action") as action:
            plugin._scheduler_tick(self.logger)
            self.now += timedelta(hours=24)
            self.settings["refresh_interval_hours"] = 0
            plugin._scheduler_tick(self.logger)
            action.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_lock_contention_neither_launches_nor_logs_another_tick(self):
        with (self.plugin_dir / ".download.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(plugin.Plugin, "_run_action") as action:
                plugin._scheduler_tick(self.logger)
                result = self.instance.run("download_epg", {}, {"settings": self.settings})
                self.assertEqual(result["status"], "skipped")
                action.assert_not_called()
        self.assertEqual(self.logger.mock_calls, [])
        self.assertFalse(self.state_path.exists())

    def test_cli_skip_preserves_workspace_and_nonzero_failure_is_an_error(self):
        for returncode, status in ((plugin.LOCK_BUSY_EXIT, "skipped"), (1, "error")):
            with self.subTest(returncode=returncode), patch.dict(os.environ, {"DISPATCHARR_DATA_DIR": self.directory}), \
                    patch("subprocess.Popen", return_value=SimpleNamespace(stdout=[], wait=lambda: returncode)), \
                    patch.object(self.instance, "_cleanup_plugin_workspace") as cleanup, \
                    patch.object(self.instance, "_create_or_update_epg_source") as update:
                result = self.instance.run("download_epg", {}, {"settings": self.settings, "logger": self.logger})
                self.assertEqual(result["status"], status)
                self.assertEqual(cleanup.call_count, int(status == "error"))
                update.assert_not_called()
                self.assertNotIn("last_generated", json.loads(self.state_path.read_text()))


if __name__ == "__main__":
    unittest.main()
