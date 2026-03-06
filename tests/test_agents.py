import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.agents import (
    _tick_lock_path,
    _try_lock,
    _upsert_cron_line,
    control_agent,
    install_cron,
    nudge_agent,
    read_agent,
    render_cron_line,
    send_agent,
    show_agent,
    start_agent,
    tick,
    write_tick_wrapper,
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

            before = show_agent(agent["id"])
            self.assertEqual(before["unread_message_count"], 1)
            self.assertEqual(before["state"]["unread_message_count"], 1)
            queued = read_agent(agent["id"])
            self.assertEqual(queued["items"][0]["kind"], "queued")
            self.assertEqual(queued["items"][0]["text"], "status")

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

    def test_write_tick_wrapper_pins_home_and_python(self):
        with _temp_home() as home:
            wrapper = write_tick_wrapper(
                home=home,
                python_executable="/tmp/venv/bin/python",
                path_value="/tmp/venv/bin:/usr/bin",
            )
            text = wrapper.read_text(encoding="utf-8")
            self.assertIn("export CODEXAPI_HOME=", text)
            self.assertIn(str(home), text)
            self.assertIn("export PATH=", text)
            self.assertIn("/tmp/venv/bin:/usr/bin", text)
            self.assertIn("exec /tmp/venv/bin/python -m codexapi agent tick", text)

    def test_upsert_cron_line_keeps_different_homes_separate(self):
        line_a = render_cron_line(home="/tmp/home-a", hostname="host-a")
        line_b = render_cron_line(home="/tmp/home-b", hostname="host-a")
        updated, changed = _upsert_cron_line("", line_a, "codexapi-agent::host-a::aaa")
        self.assertTrue(changed)
        updated, changed = _upsert_cron_line(updated, line_b, "codexapi-agent::host-a::bbb")
        self.assertTrue(changed)
        self.assertIn("/tmp/home-a/bin/agent-tick", updated)
        self.assertIn("/tmp/home-b/bin/agent-tick", updated)

    def test_install_cron_writes_wrapper_and_updates_crontab_text(self):
        writes = []

        def fake_read():
            return ""

        def fake_write(text):
            writes.append(text)

        with _temp_home() as home:
            with patch("codexapi.agents._read_crontab", fake_read):
                with patch("codexapi.agents._write_crontab", fake_write):
                    result = install_cron(
                        home=home,
                        hostname="host-a",
                        python_executable="/tmp/venv/bin/python",
                        path_value="/tmp/venv/bin:/usr/bin",
                    )
            wrapper = Path(result["wrapper"])
            self.assertTrue(wrapper.exists())
            self.assertEqual(len(writes), 1)
            self.assertIn(str(wrapper), writes[0])
            self.assertIn("codexapi-agent::host-a::", writes[0])

    def test_nudge_agent_runs_immediately_and_updates_token_totals(self):
        start = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Handled",
                        "continue": True,
                        "reply": "Message handled.",
                    }
                ),
                "thread_id": "thread-usage",
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 20,
                    "total_tokens": 50,
                },
            }

        with _temp_home():
            agent = start_agent(
                "Handle messages.",
                hostname="host-a",
                now=start,
            )
            send_agent(agent["id"], "ping", hostname="host-a", now=start)
            with patch("codexapi.agents.utc_now", return_value=end):
                result = nudge_agent(
                    agent["id"],
                    hostname="host-a",
                    now=start + timedelta(seconds=10),
                    runner=fake_runner,
                )
            self.assertTrue(result["ran"])
            self.assertEqual(result["woken"], 1)
            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["thread_id"], "thread-usage")
            self.assertEqual(shown["state"]["input_tokens"], 30)
            self.assertEqual(shown["state"]["output_tokens"], 20)
            self.assertEqual(shown["state"]["total_tokens"], 50)
            self.assertEqual(shown["state"]["avg_tokens_per_hour"], 50.0)
            self.assertEqual(shown["state"]["reply"], "Message handled.")
            self.assertEqual(shown["unread_message_count"], 0)


if __name__ == "__main__":
    unittest.main()
