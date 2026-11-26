import logging
import sys
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from src.zap2xml import plugin


class _LoopExit(Exception):
    """Stand-in for gevent.exceptions.LoopExit; gevent is absent in the test env."""


class _OperationalError(Exception):
    """Stand-in for django.db.OperationalError; Django is absent in the test env."""


class _StopLoop(Exception):
    """Ends a scripted run of _scheduler_loop."""


def _stub_django(connection):
    db = ModuleType("django.db")
    db.OperationalError = _OperationalError
    db.connection = connection
    db.close_old_connections = Mock()
    django = ModuleType("django")
    django.db = db
    return django, db


def _stub_gevent():
    exceptions = ModuleType("gevent.exceptions")
    exceptions.LoopExit = _LoopExit
    gevent = ModuleType("gevent")
    gevent.exceptions = exceptions
    return {"gevent": gevent, "gevent.exceptions": exceptions}


class SchedulerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(plugin, "_ensure_scheduler_started"))
        self.enterContext(patch.object(plugin, "_db_busy_warned_at", 0.0))
        self.config = SimpleNamespace(enabled=True, settings={})
        self.config_model = SimpleNamespace(objects=Mock())
        self.config_model.objects.filter.return_value.first.return_value = self.config
        self.enterContext(patch.dict(sys.modules, {
            "apps.plugins.models": SimpleNamespace(PluginConfig=self.config_model),
        }))
        self.connection = Mock()
        self.django, self.db = _stub_django(self.connection)
        self.enterContext(patch.dict(sys.modules, {"django": self.django, "django.db": self.db}))
        self.logger = Mock(spec=logging.Logger)

    def _drive_loop(self, outcomes):
        """Run _scheduler_loop once per outcome, then stop from its sleep()."""
        pending = list(outcomes)

        def tick(_logger):
            if not pending:
                raise AssertionError("scheduler loop ran more ticks than scripted")
            outcome = pending.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome

        def sleep(_seconds):
            if not pending:
                raise _StopLoop

        clock = SimpleNamespace(sleep=sleep, monotonic=time.monotonic)
        with patch.object(plugin, "_scheduler_tick", side_effect=tick), \
                patch.object(plugin, "time", clock), \
                patch.object(logging, "getLogger", return_value=self.logger):
            with self.assertRaises(_StopLoop):
                plugin._scheduler_loop()

    def test_tick_closes_the_thread_connection_before_and_after_the_orm_work(self):
        calls = []
        self.db.close_old_connections.side_effect = lambda: calls.append("close_old_connections")
        self.config_model.objects.filter.side_effect = lambda **_: (
            calls.append("query"), self.config_model.objects.filter.return_value
        )[1]
        with patch.object(plugin.Plugin, "_download", return_value={"status": "queued"}) as download:
            plugin._scheduler_tick(self.logger)
        self.assertEqual(calls, ["close_old_connections", "query"])
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.connection.close.call_count, 1)

    def test_tick_closes_the_thread_connection_after_an_error(self):
        with patch.object(plugin.Plugin, "_download", side_effect=RuntimeError("download exploded")):
            with self.assertRaises(RuntimeError):
                plugin._scheduler_tick(self.logger)
        self.assertEqual(self.connection.close.call_count, 1)

    def test_connection_operational_error_is_raised_as_database_busy(self):
        self.config_model.objects.filter.side_effect = _OperationalError("could not connect to server")
        with self.assertRaises(plugin._DatabaseBusy):
            plugin._scheduler_tick(self.logger)
        self.assertEqual(self.connection.close.call_count, 1)

    def test_disabled_schedule_still_closes_the_connection(self):
        self.config.enabled = False
        plugin._scheduler_tick(self.logger)
        self.assertEqual(self.connection.close.call_count, 1)

    def test_loop_exit_warns_once_per_hour_without_a_traceback(self):
        with patch.dict(sys.modules, _stub_gevent()):
            self._drive_loop([_LoopExit("This operation would block forever"), _LoopExit("still busy")])
            self.assertEqual(self.logger.warning.call_count, 1)
            self.assertEqual(
                self.logger.warning.call_args.args,
                ("[zap2xml] Scheduler: database pool busy; retrying next minute",),
            )
            self.assertFalse(self.logger.warning.call_args.kwargs)
            self.logger.exception.assert_not_called()

            plugin._db_busy_warned_at = time.monotonic() - plugin._DB_BUSY_WARNING_INTERVAL
            self._drive_loop([_LoopExit("an hour later")])
            self.assertEqual(self.logger.warning.call_count, 2)

    def test_database_busy_from_the_tick_is_the_same_warning(self):
        self.config_model.objects.filter.side_effect = _OperationalError("connection pool exhausted")

        def sleep(_seconds):
            raise _StopLoop

        clock = SimpleNamespace(sleep=sleep, monotonic=time.monotonic)
        with patch.dict(sys.modules, _stub_gevent()), patch.object(plugin, "time", clock), \
                patch.object(logging, "getLogger", return_value=self.logger):
            with self.assertRaises(_StopLoop):
                plugin._scheduler_loop()
        self.assertEqual(self.logger.warning.call_count, 1)
        self.logger.exception.assert_not_called()
        self.assertEqual(self.connection.close.call_count, 1)

    def test_any_other_error_keeps_the_traceback(self):
        with patch.dict(sys.modules, _stub_gevent()):
            self._drive_loop([RuntimeError("boom")])
        self.logger.exception.assert_called_once_with("[zap2xml] Scheduler loop error")
        self.logger.warning.assert_not_called()

    def test_without_gevent_only_database_busy_is_transient(self):
        self.assertFalse(plugin._pool_busy(RuntimeError("boom")))
        self.assertTrue(plugin._pool_busy(plugin._DatabaseBusy("no connection")))


if __name__ == "__main__":
    unittest.main()
