import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.task import Task


class _FakeAgent:
    def __init__(self):
        self.thread_id = "thread-123"

    def __call__(self, prompt):
        return "ok"


class _FakePushover:
    def ensure_ready(self, announce=True):
        return False

    def send(self, title, message):
        return False


class _ImmediateSuccessTask(Task):
    def __init__(self):
        super().__init__("do the thing", max_iterations=3)
        self.agent = _FakeAgent()
        self._pushover = _FakePushover()
        self.set_up_called = False
        self.tear_down_called = False

    def set_up(self):
        self.set_up_called = True

    def tear_down(self):
        self.tear_down_called = True

    def check(self, output=None):
        self.last_check_output = '{"success": true, "reason": "ok"}'
        self.check_skipped = False
        return None

    def notify_pushover(self, result):
        return None


class TaskProgressEstimateFailureTests(unittest.TestCase):
    def test_progress_does_not_crash_when_initial_estimate_fails(self):
        task = _ImmediateSuccessTask()
        with patch("codexapi.task.estimate", side_effect=RuntimeError("bad json")):
            result = task(progress=True)
        self.assertTrue(result.success)
        self.assertEqual(result.iterations, 1)
        self.assertTrue(task.set_up_called)
        self.assertTrue(task.tear_down_called)

    def test_progress_does_not_crash_when_later_estimate_fails(self):
        task = _ImmediateSuccessTask()
        with patch(
            "codexapi.task.estimate",
            side_effect=[(5, "initial"), RuntimeError("bad json")],
        ):
            result = task(progress=True)
        self.assertTrue(result.success)
        self.assertEqual(result.iterations, 1)
        self.assertTrue(task.set_up_called)
        self.assertTrue(task.tear_down_called)


if __name__ == "__main__":
    unittest.main()
