import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.agents import (
    _codex_rollout_usage,
    _tick_lock_path,
    _try_lock,
    _remove_cron_line,
    _upsert_cron_line,
    control_agent,
    format_utc,
    install_cron,
    nudge_agent,
    read_agent,
    render_cron_line,
    send_agent,
    show_agent,
    start_agent,
    tick,
    uninstall_cron,
    write_tick_wrapper,
)
from codexapi.cli import (
    _print_managed_agent_identity,
    _print_managed_agent_list,
    _print_managed_agent_show,
)


@contextmanager
def _temp_home():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"CODEXAPI_HOME": tmpdir, "USER": "tester"}, clear=False):
            yield Path(tmpdir)


class AgentsTests(unittest.TestCase):
    def test_current_hostname_prefers_override(self):
        with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "stable-host"}, clear=False):
            from codexapi.agents import current_hostname

            self.assertEqual(current_hostname(), "stable-host")

    def test_cli_whoami_shows_effective_host_and_home(self):
        with _temp_home() as home:
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "stable-host"}, clear=False):
                output = io.StringIO()
                with redirect_stdout(output):
                    _print_managed_agent_identity()
        text = output.getvalue()
        self.assertIn("Host: stable-host", text)
        self.assertIn("Host override: stable-host", text)
        self.assertIn(f"Home: {home.resolve()}", text)

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
            self.assertEqual(conversation["items"][0]["kind"], "user")
            self.assertEqual(conversation["items"][0]["author"], "mark")
            self.assertEqual(conversation["items"][0]["text"], "status")
            self.assertEqual(conversation["items"][1]["kind"], "agent")
            self.assertEqual(conversation["items"][1]["text"], "I saw your message.")

    def test_start_agent_resolves_parent_ref(self):
        with _temp_home():
            parent = start_agent(
                "Parent work.",
                name="parent-agent",
                hostname="host-a",
            )
            child = start_agent(
                "Child work.",
                name="child-agent",
                parent_ref="parent-agent",
                hostname="host-a",
            )
            shown = show_agent(child["id"])
            self.assertEqual(shown["meta"]["parent_id"], parent["id"])
            self.assertEqual(shown["meta"]["created_by"], "parent-agent")
            self.assertEqual(shown["parent"]["id"], parent["id"])

    def test_managed_agent_can_create_child_with_parent_defaults(self):
        start = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        captured = {}

        with _temp_home():
            parent = start_agent(
                "Spawn a child agent.",
                name="parent-agent",
                hostname="host-a",
                now=start,
            )

            class FakeAgent:
                def __init__(
                    self,
                    cwd=None,
                    yolo=True,
                    thread_id=None,
                    flags=None,
                    include_thinking=False,
                    backend=None,
                    env=None,
                ):
                    self.thread_id = thread_id
                    self.last_usage = {}
                    self.env = dict(env or {})
                    captured["env"] = self.env

                def __call__(self, prompt):
                    with patch.dict(os.environ, self.env, clear=False):
                        child = start_agent(
                            "Child work.",
                            name="child-agent",
                            hostname="host-a",
                        )
                    captured["child_id"] = child["id"]
                    return json.dumps(
                        {
                            "status": "Spawned child",
                            "continue": False,
                            "reply": child["id"],
                        }
                    )

            with patch("codexapi.agents.Agent", FakeAgent):
                with patch(
                    "codexapi.agents.utc_now",
                    side_effect=[start, start + timedelta(seconds=1), end],
                ):
                    result = nudge_agent(
                        parent["id"],
                        hostname="host-a",
                        now=start,
                    )
            self.assertTrue(result["ran"])
            self.assertEqual(result["woken"], 1)
            self.assertEqual(captured["env"]["CODEXAPI_AGENT_ID"], parent["id"])
            self.assertEqual(captured["env"]["CODEXAPI_AGENT_NAME"], "parent-agent")

            child = show_agent(captured["child_id"])
            self.assertEqual(child["meta"]["created_by"], "parent-agent")
            self.assertEqual(child["meta"]["parent_id"], parent["id"])
            self.assertEqual(child["parent"]["name"], "parent-agent")

            parent_view = show_agent(parent["id"])
            self.assertIn(captured["child_id"], parent_view["child_ids"])
            self.assertEqual(parent_view["children"][0]["name"], "child-agent")

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
                hostname="stable-host",
            )
            text = wrapper.read_text(encoding="utf-8")
            self.assertIn("export CODEXAPI_HOME=", text)
            self.assertIn(str(home), text)
            self.assertIn("export CODEXAPI_HOSTNAME=stable-host", text)
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

    def test_remove_cron_line_keeps_other_entries(self):
        existing = (
            "* * * * * /tmp/home-a/bin/agent-tick >/dev/null 2>&1  # codexapi-agent::host-a::aaa\n"
            "* * * * * /tmp/home-b/bin/agent-tick >/dev/null 2>&1  # codexapi-agent::host-a::bbb\n"
        )
        updated, changed = _remove_cron_line(existing, "codexapi-agent::host-a::aaa")
        self.assertTrue(changed)
        self.assertNotIn("/tmp/home-a/bin/agent-tick", updated)
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

    def test_install_cron_is_idempotent_when_line_already_matches(self):
        writes = []

        with _temp_home() as home:
            expected_line = render_cron_line(home=home, hostname="host-a")

            def fake_read():
                return expected_line + "\n"

            def fake_write(text):
                writes.append(text)

            with patch("codexapi.agents._read_crontab", fake_read):
                with patch("codexapi.agents._write_crontab", fake_write):
                    result = install_cron(
                        home=home,
                        hostname="host-a",
                        python_executable="/tmp/venv/bin/python",
                        path_value="/tmp/venv/bin:/usr/bin",
                    )
            self.assertFalse(result["changed"])
            self.assertEqual(writes, [])

    def test_uninstall_cron_removes_only_this_home_entry_and_wrapper(self):
        writes = []

        def fake_write(text):
            writes.append(text)

        with _temp_home() as home:
            wrapper = write_tick_wrapper(
                home=home,
                python_executable="/tmp/venv/bin/python",
                path_value="/tmp/venv/bin:/usr/bin",
            )
            record = home / "cron" / "agent.cron"
            record.write_text("placeholder\n", encoding="utf-8")
            this_line = render_cron_line(home=home, hostname="host-a")
            other_line = render_cron_line(home="/tmp/other-home", hostname="host-a")

            def fake_read():
                return this_line + "\n" + other_line + "\n"

            with patch("codexapi.agents._read_crontab", fake_read):
                with patch("codexapi.agents._write_crontab", fake_write):
                    result = uninstall_cron(home=home, hostname="host-a")
            self.assertTrue(result["changed"])
            self.assertFalse(wrapper.exists())
            self.assertFalse(record.exists())
            self.assertEqual(len(writes), 1)
            self.assertNotIn(str(wrapper), writes[0])
            self.assertIn("/tmp/other-home/bin/agent-tick", writes[0])

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

    def test_codex_rollout_usage_uses_latest_event_after_start(self):
        started = datetime(2026, 3, 6, 8, 0, 5, tzinfo=timezone.utc)
        with _temp_home() as home:
            codex_home = home / "codex-home"
            rollout = (
                codex_home
                / "sessions"
                / "2026"
                / "03"
                / "06"
                / "rollout-2026-03-06T09-00-00-thread-rollout.jsonl"
            )
            rollout.parent.mkdir(parents=True, exist_ok=True)
            events = [
                {
                    "timestamp": "2026-03-06T08:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "total_tokens": 15,
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-03-06T08:00:06Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 20,
                                "output_tokens": 10,
                                "total_tokens": 30,
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-03-06T08:00:07Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 40,
                                "output_tokens": 12,
                                "total_tokens": 52,
                            }
                        },
                    },
                },
            ]
            rollout.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                usage, path = _codex_rollout_usage(
                    {"rollout_path": str(rollout)},
                    "thread-rollout",
                    started,
                )
            self.assertEqual(path, str(rollout))
            self.assertEqual(
                usage,
                {"input_tokens": 40, "output_tokens": 12, "total_tokens": 52},
            )

    def test_nudge_agent_reads_usage_from_codex_rollout(self):
        start = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        with _temp_home() as home:
            codex_home = home / "codex-home"

            class FakeAgent:
                def __init__(
                    self,
                    cwd=None,
                    yolo=True,
                    thread_id=None,
                    flags=None,
                    include_thinking=False,
                    backend=None,
                    env=None,
                ):
                    self.thread_id = thread_id
                    self.last_usage = {}

                def __call__(self, prompt):
                    self.thread_id = "thread-rollout"
                    rollout = (
                        codex_home
                        / "sessions"
                        / "2026"
                        / "03"
                        / "06"
                        / "rollout-2026-03-06T09-00-00-thread-rollout.jsonl"
                    )
                    rollout.parent.mkdir(parents=True, exist_ok=True)
                    events = [
                        {
                            "timestamp": format_utc(start + timedelta(seconds=5)),
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 40,
                                        "output_tokens": 10,
                                        "total_tokens": 50,
                                    }
                                },
                            },
                        }
                    ]
                    rollout.write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n",
                        encoding="utf-8",
                    )
                    return json.dumps(
                        {
                            "status": "Handled from rollout",
                            "continue": False,
                            "reply": "Used rollout tokens.",
                        }
                    )

            agent = start_agent(
                "Handle with real rollout accounting.",
                hostname="host-a",
                now=start,
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                with patch("codexapi.agents.Agent", FakeAgent):
                    with patch("codexapi.agents.utc_now", side_effect=[start, end]):
                        result = nudge_agent(
                            agent["id"],
                            hostname="host-a",
                            now=start,
                        )
            self.assertTrue(result["ran"])
            self.assertEqual(result["woken"], 1)
            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["thread_id"], "thread-rollout")
            self.assertEqual(shown["state"]["input_tokens"], 40)
            self.assertEqual(shown["state"]["output_tokens"], 10)
            self.assertEqual(shown["state"]["total_tokens"], 50)
            self.assertEqual(shown["state"]["avg_tokens_per_hour"], 50.0)
            self.assertEqual(shown["state"]["reply"], "Used rollout tokens.")
            self.assertIn("thread-rollout", shown["session"]["rollout_path"])

    def test_cli_managed_agent_views_show_operator_fields(self):
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
                nudge_agent(
                    agent["id"],
                    hostname="host-a",
                    now=start + timedelta(seconds=10),
                    runner=fake_runner,
                )
            shown = show_agent(agent["id"])
            list_out = io.StringIO()
            with redirect_stdout(list_out):
                _print_managed_agent_list([shown])
            self.assertIn("POL", list_out.getvalue())
            self.assertIn("REPO", list_out.getvalue())
            self.assertIn("done", list_out.getvalue())
            self.assertIn("codexapi", list_out.getvalue())

            show_out = io.StringIO()
            with redirect_stdout(show_out):
                _print_managed_agent_show(shown)
            text = show_out.getvalue()
            self.assertIn("Policy: until_done", text)
            self.assertIn("Tokens: 50 total (30 in, 20 out, 50.0/h)", text)
            self.assertIn("Prompt: Handle messages.", text)
            self.assertIn("Recent runs:", text)
            self.assertIn("msgs=1", text)


if __name__ == "__main__":
    unittest.main()
