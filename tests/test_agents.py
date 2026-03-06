import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.agents import (
    _tick_lock_path,
    _try_lock,
    control_agent,
    read_agent,
    send_agent,
    show_agent,
    start_agent,
    tick,
)


@contextmanager
def _temp_home():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"CODEXAPI_HOME": tmpdir, "USER": "tester"}, clear=False):
            yield Path(tmpdir)


class AgentsTests(unittest.TestCase):
    def test_homes_are_isolated(self):
        with _temp_home() as home_a:
            first = start_agent("Monitor the build queue.", hostname="host-a")
            self.assertEqual(first["name"], "monitor-the-build-queue")
            agents_a = show_agent(first["id"])
            self.assertEqual(agents_a["meta"]["hostname"], "host-a")
        with _temp_home() as home_b:
            with self.assertRaises(ValueError):
                show_agent(first["id"])
            second = start_agent("Watch CI failures.", hostname="host-b")
            self.assertNotEqual(first["id"], second["id"])

    def test_cross_host_message_waits_for_owner_tick(self):
        prompts = []

        def fake_runner(meta, session, prompt):
            prompts.append(prompt)
            return {
                "message": json.dumps(
                    {
                        "status": "Replied",
                        "continue": False,
                        "reply": "I saw your message.",
                    }
                ),
                "thread_id": "thread-abc",
            }

        with _temp_home():
            agent = start_agent("Handle background work.", hostname="host-a")
            send_agent(agent["id"], "status", author="mark", hostname="host-b")

            other_host = tick(hostname="host-b", runner=fake_runner)
            self.assertTrue(other_host["ran"])
            self.assertEqual(other_host["processed"], 0)
            self.assertEqual(other_host["woken"], 0)

            owner = tick(hostname="host-a", runner=fake_runner)
            self.assertTrue(owner["ran"])
            self.assertEqual(owner["woken"], 1)
            self.assertEqual(len(prompts), 1)
            self.assertIn("mark: status", prompts[0])

            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["status"], "done")
            self.assertEqual(shown["state"]["thread_id"], "thread-abc")
            self.assertEqual(shown["state"]["reply"], "I saw your message.")
            self.assertEqual(shown["state"]["unread_message_count"], 0)

            conversation = read_agent(agent["id"])
            self.assertEqual(conversation["items"][0]["kind"], "agent")
            self.assertEqual(conversation["items"][0]["text"], "I saw your message.")

    def test_pause_then_resume(self):
        calls = []

        def fake_runner(meta, session, prompt):
            calls.append(prompt)
            return {
                "message": json.dumps(
                    {
                        "status": "Still running",
                        "continue": True,
                        "reply": "Continuing.",
                    }
                ),
                "thread_id": "thread-xyz",
            }

        with _temp_home():
            agent = start_agent("Keep an eye on this.", hostname="host-a")
            control_agent(agent["id"], "pause", hostname="host-b")
            paused = tick(hostname="host-a", runner=fake_runner)
            self.assertEqual(paused["processed"], 1)
            self.assertEqual(paused["woken"], 0)
            self.assertEqual(show_agent(agent["id"])["state"]["status"], "paused")
            self.assertEqual(calls, [])

            control_agent(agent["id"], "resume", hostname="host-b")
            resumed = tick(hostname="host-a", runner=fake_runner)
            self.assertEqual(resumed["woken"], 1)
            self.assertEqual(len(calls), 1)
            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["status"], "ready")
            self.assertEqual(shown["state"]["thread_id"], "thread-xyz")

    def test_tick_lock_is_non_blocking(self):
        with _temp_home() as home:
            start_agent("Do the thing.", hostname="host-a")
            lock_path = _tick_lock_path(home, "host-a")
            with _try_lock(lock_path) as handle:
                self.assertIsNotNone(handle)
                result = tick(hostname="host-a")
            self.assertFalse(result["ran"])
            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["woken"], 0)


if __name__ == "__main__":
    unittest.main()
