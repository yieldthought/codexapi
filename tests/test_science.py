import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.science import Science, _science_parts


class _FakePushover:
    def __init__(self, enabled):
        self.enabled = enabled
        self.sent = []

    def ensure_ready(self, announce=True):
        return self.enabled

    def send(self, title, message):
        self.sent.append((title, message))
        return True


class _TestScience(Science):
    def _append_logbook(self, iteration, message):
        return None

    def _extract_and_notify(self, message):
        return None

    def _build_run_title(self):
        return "test-run"


class _FakeAgent:
    calls = 0

    def __init__(
        self,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
        welfare=False,
        include_thinking=False,
        backend=None,
    ):
        pass

    def __call__(self, prompt):
        _FakeAgent.calls += 1
        return f"message {_FakeAgent.calls}"


class ScienceTests(unittest.TestCase):
    def test_science_prompt_includes_git_commit_guidance(self):
        _prompt_a, prompt_b = _science_parts("improve performance")
        self.assertIn("create and use a local branch", prompt_b)
        self.assertIn("never commit or reset LOGBOOK.md or SCIENCE.md", prompt_b)

    def test_max_duration_stops_after_current_iteration(self):
        _FakeAgent.calls = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _TestScience(
                "improve performance",
                cwd=tmpdir,
                max_duration_seconds=60,
            )
            runner._pushover = _FakePushover(enabled=False)
            with patch("codexapi.ralph.Agent", _FakeAgent):
                with patch("codexapi.science.time.monotonic", side_effect=[0, 30, 61]):
                    runner()
        self.assertEqual(_FakeAgent.calls, 2)
        self.assertTrue(runner._duration_limit_hit)
        self.assertEqual(runner._last_iteration, 2)

    def test_final_pushover_update_sent_when_enabled(self):
        _FakeAgent.calls = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _TestScience(
                "improve performance",
                cwd=tmpdir,
                max_iterations=1,
            )
            fake_pushover = _FakePushover(enabled=True)
            runner._pushover = fake_pushover
            with patch("codexapi.ralph.Agent", _FakeAgent):
                runner()
        self.assertEqual(len(fake_pushover.sent), 1)
        title, message = fake_pushover.sent[0]
        self.assertEqual(title, "test-run")
        self.assertIn("Science run ended: max iterations reached (1)", message)
        self.assertIn("Iterations completed: 1", message)


if __name__ == "__main__":
    unittest.main()
