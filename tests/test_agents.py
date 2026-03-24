import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi import __version__
from codexapi.agents import (
    _build_wake_prompt,
    _codex_rollout_usage,
    _tick_lock_path,
    _try_lock,
    _remove_cron_line,
    _upsert_cron_line,
    control_agent,
    cron_installed,
    cron_status,
    delete_agent,
    format_utc,
    install_cron,
    nudge_agent,
    read_agent,
    read_agentbook,
    recover_agent,
    render_cron_line,
    send_agent,
    set_agent_heartbeat,
    show_agent,
    start_agent,
    status_agent,
    tick,
    uninstall_cron,
    write_tick_wrapper,
)
from codexapi.cli import (
    _print_managed_agent_identity,
    _print_managed_agent_list,
    _print_managed_agent_show,
    _print_managed_agent_status,
    main as cli_main,
)
from codexapi.lead import _leadbook_block


@contextmanager
def _temp_home():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"CODEXAPI_HOME": tmpdir, "USER": "tester"}, clear=False):
            yield Path(tmpdir)


def _write_rollout(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _set_rollout_session(home, agent_id, hostname, thread_id, rollout_path):
    session_path = home / "agents" / agent_id / "hosts" / hostname / "session.json"
    state_path = home / "agents" / agent_id / "state.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    session["thread_id"] = thread_id
    session["rollout_path"] = str(rollout_path)
    state["thread_id"] = thread_id
    session_path.write_text(json.dumps(session, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class AgentsTests(unittest.TestCase):
    def test_cli_version(self):
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as exc:
                cli_main(["--version"])
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"codexapi {__version__}")

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
            self.assertTrue(agents_a["agentbook_path"].endswith("/AGENTBOOK.md"))
        with _temp_home() as home_b:
            with self.assertRaises(ValueError):
                show_agent(first["id"])
            second = start_agent("Watch CI failures.", hostname="host-b")
            self.assertNotEqual(first["id"], second["id"])

    def test_read_agentbook_and_cli_book(self):
        with _temp_home():
            agent = start_agent("Keep notes.", hostname="host-a")
            book = read_agentbook(agent["id"])
            self.assertTrue(book["path"].endswith("/AGENTBOOK.md"))
            self.assertIn("# Agentbook", book["text"])
            self.assertIn("## Purpose", book["text"])
            self.assertIn("## Values", book["text"])
            self.assertIn("## Original Goal", book["text"])
            self.assertIn("Keep notes.", book["text"])

            output = io.StringIO()
            with redirect_stdout(output):
                cli_main(["agent", "book", agent["id"]])
            text = output.getvalue()
            self.assertIn("Agentbook:", text)
            self.assertIn("# Agentbook", text)

    def test_build_wake_prompt_shows_agentbook_header_and_latest_notes(self):
        with _temp_home() as home:
            agent = start_agent("Watch for the real issue.", hostname="host-a")
            agent_dir = home / "agents" / agent["id"]
            book_path = agent_dir / "AGENTBOOK.md"
            book_path.write_text(
                "\n".join(
                    [
                        "# Agentbook",
                        "",
                        "## Purpose",
                        "- We are here to achieve the goal, not to appear to make progress.",
                        "",
                        "## Values",
                        "- Hold the whole.",
                        "- Seek the real shape.",
                        "",
                        "## Original Goal",
                        "```text",
                        "Watch for the real issue.",
                        "```",
                        "",
                        "## Standing Guidance",
                        "- Prefer the truer explanation to the tidier one.",
                        "",
                        "## Working Notes",
                        "",
                        "### 2026-03-23 08:00 UTC",
                        "- OLD " + ("alpha " * 500),
                        "",
                        "### 2026-03-23 09:00 UTC",
                        "- NEW " + ("omega " * 120),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            meta = json.loads((agent_dir / "meta.json").read_text(encoding="utf-8"))
            state = json.loads((agent_dir / "state.json").read_text(encoding="utf-8"))
            session = json.loads((agent_dir / "hosts" / "host-a" / "session.json").read_text(encoding="utf-8"))
            state["last_success_at"] = "2026-03-23T08:30:00Z"
            state["activity"] = "Watching"
            state["update"] = "Still narrowing the field."
            session["thread_id"] = "thread-123"
            prompt = _build_wake_prompt(
                meta,
                state,
                session,
                datetime(2026, 3, 23, 9, 30, tzinfo=timezone.utc),
                [],
                agent_dir,
            )
            self.assertIn("Agentbook (header + latest notes):", prompt)
            self.assertIn("## Purpose", prompt)
            self.assertIn("Hold the whole.", prompt)
            self.assertIn("Watch for the real issue.", prompt)
            self.assertIn("NEW omega", prompt)
            self.assertIn("Previous status: Watching", prompt)
            self.assertIn("Previous update: Still narrowing the field.", prompt)
            self.assertIn("[... older notes omitted ...]", prompt)
            self.assertNotIn("Original instructions:", prompt)
            self.assertNotIn("OLD alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha", prompt)

    def test_build_wake_prompt_repairs_legacy_agentbook_before_wake(self):
        with _temp_home() as home:
            agent = start_agent("Keep the true goal in view.", hostname="host-a")
            agent_dir = home / "agents" / agent["id"]
            book_path = agent_dir / "AGENTBOOK.md"
            book_path.write_text(
                "\n".join(
                    [
                        "# Agentbook",
                        "",
                        "Use this file as the durable working memory for the agent.",
                        "Append dated notes as work progresses.",
                        "Keep entries short and concrete.",
                        "",
                        "## 2026-03-23 08:00 UTC",
                        "- Legacy note about the real issue.",
                    ]
                ),
                encoding="utf-8",
            )
            meta = json.loads((agent_dir / "meta.json").read_text(encoding="utf-8"))
            state = json.loads((agent_dir / "state.json").read_text(encoding="utf-8"))
            session = json.loads((agent_dir / "hosts" / "host-a" / "session.json").read_text(encoding="utf-8"))
            state["last_success_at"] = "2026-03-23T08:30:00Z"
            session["thread_id"] = "thread-legacy"
            now = datetime(2026, 3, 23, 9, 30, tzinfo=timezone.utc)
            prompt = _build_wake_prompt(meta, state, session, now, [], agent_dir)
            repaired = book_path.read_text(encoding="utf-8")
            self.assertIn("## Purpose", repaired)
            self.assertIn("## Values", repaired)
            self.assertIn("## Original Goal", repaired)
            self.assertIn("Keep the true goal in view.", repaired)
            self.assertIn("### 2026-03-23 09:30 UTC", repaired)
            self.assertIn("The durable agentbook header was restored automatically on wake", repaired)
            self.assertIn("Legacy note about the real issue.", repaired)
            self.assertIn("## Purpose", prompt)
            self.assertIn("Keep the true goal in view.", prompt)
            self.assertNotIn("Original instructions:", prompt)

    def test_leadbook_block_shows_header_and_latest_notes(self):
        leadbook = "\n".join(
            [
                "# Leadbook — Studio Notes",
                "",
                "Aim:",
                "- Move the true work forward.",
                "",
                "Signals:",
                "- Treat oddities as clues.",
                "",
                "## 2026-03-23 08:00",
                "- OLD " + ("alpha " * 350),
                "",
                "## 2026-03-23 09:00",
                "- NEW " + ("omega " * 80),
            ]
        )
        block = _leadbook_block("/tmp/LEADBOOK.md", leadbook)
        self.assertIn("Leadbook (header + latest notes):", block)
        self.assertIn("Aim:", block)
        self.assertIn("Signals:", block)
        self.assertIn("NEW omega", block)
        self.assertIn("[... older notes omitted ...]", block)
        self.assertNotIn("OLD alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha", block)

    def test_delete_agent_removes_done_agent(self):
        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Finished",
                        "continue": False,
                        "reply": "done",
                    }
                ),
                "thread_id": "thread-delete",
            }

        with _temp_home():
            agent = start_agent("Finish then delete.", hostname="host-a")
            tick(hostname="host-a", runner=fake_runner)
            result = delete_agent(agent["id"])
            self.assertTrue(result["deleted"])
            with self.assertRaises(ValueError):
                show_agent(agent["id"])

    def test_delete_agent_refuses_non_terminal_without_force(self):
        with _temp_home():
            agent = start_agent("Do not delete me yet.", hostname="host-a")
            with self.assertRaises(ValueError):
                delete_agent(agent["id"])
            result = delete_agent(agent["id"], force=True)
            self.assertTrue(result["forced"])

    def test_delete_agent_refuses_when_run_lock_held(self):
        with _temp_home() as home:
            agent = start_agent("Locked agent.", hostname="host-a")
            agent_dir = home / "agents" / agent["id"]
            lock_path = agent_dir / "hosts" / "host-a" / "run.lock"
            with _try_lock(lock_path) as handle:
                self.assertIsNotNone(handle)
                with self.assertRaises(ValueError):
                    delete_agent(agent["id"], force=True)

    def test_cli_delete_removes_agent(self):
        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Finished",
                        "continue": False,
                        "reply": "done",
                    }
                ),
                "thread_id": "thread-delete-cli",
            }

        with _temp_home():
            agent = start_agent("Finish then delete.", hostname="host-a")
            tick(hostname="host-a", runner=fake_runner)
            output = io.StringIO()
            with redirect_stdout(output):
                cli_main(["agent", "delete", agent["id"]])
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["deleted"])
            with self.assertRaises(ValueError):
                show_agent(agent["id"])

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
                    side_effect=[
                        start,
                        start + timedelta(seconds=1),
                        start + timedelta(seconds=2),
                        end,
                    ],
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

    def test_resume_done_agent_reopens_it(self):
        def finish_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Finished",
                        "continue": False,
                        "reply": "done",
                    }
                ),
                "thread_id": "thread-done",
            }

        def resume_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Back on it",
                        "continue": True,
                        "reply": "Reopened.",
                    }
                ),
                "thread_id": "thread-done",
            }

        with _temp_home():
            agent = start_agent("Keep an eye on this.", hostname="host-a")
            tick(hostname="host-a", runner=finish_runner)
            self.assertEqual(show_agent(agent["id"])["state"]["status"], "done")

            control_agent(agent["id"], "resume", hostname="host-b")
            resumed = tick(hostname="host-a", runner=resume_runner)
            self.assertEqual(resumed["woken"], 1)
            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["status"], "ready")
            self.assertEqual(shown["state"]["reply"], "Reopened.")

    def test_send_wakes_done_agent_once_without_reopening(self):
        prompts = []

        def finish_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Finished",
                        "continue": False,
                        "reply": "done",
                    }
                ),
                "thread_id": "thread-done",
            }

        def reply_runner(meta, session, prompt):
            prompts.append(prompt)
            return {
                "message": json.dumps(
                    {
                        "status": "Answered",
                        "continue": True,
                        "reply": "I saw your note.",
                    }
                ),
                "thread_id": "thread-done",
            }

        with _temp_home():
            agent = start_agent("Handle background work.", hostname="host-a")
            tick(hostname="host-a", runner=finish_runner)
            send_agent(agent["id"], "status", author="mark", hostname="host-b")

            result = tick(hostname="host-a", runner=reply_runner)
            self.assertEqual(result["woken"], 1)
            self.assertIn("mark: status", prompts[0])

            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["status"], "done")
            self.assertEqual(shown["state"]["reply"], "I saw your note.")
            self.assertEqual(shown["state"]["next_wake_at"], "")
            self.assertEqual(shown["state"]["unread_message_count"], 0)

    def test_send_wakes_canceled_agent_once_without_reopening(self):
        prompts = []

        def reply_runner(meta, session, prompt):
            prompts.append(prompt)
            return {
                "message": json.dumps(
                    {
                        "status": "Answered",
                        "continue": True,
                        "reply": "I saw your note.",
                    }
                ),
                "thread_id": "thread-canceled",
            }

        with _temp_home():
            agent = start_agent("Handle background work.", hostname="host-a")
            control_agent(agent["id"], "cancel", hostname="host-b")
            tick(hostname="host-a", runner=reply_runner)
            self.assertEqual(show_agent(agent["id"])["state"]["status"], "canceled")

            send_agent(agent["id"], "status", author="mark", hostname="host-b")
            result = tick(hostname="host-a", runner=reply_runner)
            self.assertEqual(result["woken"], 1)
            self.assertIn("mark: status", prompts[-1])

            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["status"], "canceled")
            self.assertEqual(shown["state"]["reply"], "I saw your note.")
            self.assertEqual(shown["state"]["next_wake_at"], "")
            self.assertEqual(shown["state"]["unread_message_count"], 0)

    def test_set_agent_heartbeat_updates_meta_and_reschedules_idle_agent(self):
        start = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=1)
        now = start + timedelta(minutes=2)

        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Handled",
                        "continue": True,
                        "reply": "Still watching.",
                    }
                ),
                "thread_id": "thread-heartbeat",
            }

        with _temp_home():
            agent = start_agent(
                "Keep an eye on this.",
                hostname="host-a",
                heartbeat_minutes=30,
                now=start,
            )
            with patch("codexapi.agents.utc_now", return_value=end):
                nudge_agent(
                    agent["id"],
                    hostname="host-a",
                    now=start,
                    runner=fake_runner,
                )
            result = set_agent_heartbeat(
                agent["id"],
                10,
                now=now,
            )
            shown = show_agent(agent["id"])
            self.assertTrue(result["changed"])
            self.assertTrue(result["rescheduled"])
            self.assertFalse(result["running"])
            self.assertEqual(result["heartbeat_minutes"], 10)
            self.assertEqual(shown["meta"]["heartbeat_minutes"], 10)
            self.assertEqual(
                shown["state"]["next_wake_at"],
                format_utc(now + timedelta(minutes=10)),
            )

    def test_set_agent_heartbeat_leaves_pending_wake_time_when_wake_requested(self):
        start = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
        now = start + timedelta(minutes=1)

        with _temp_home():
            agent = start_agent(
                "Keep an eye on this.",
                hostname="host-a",
                heartbeat_minutes=30,
                now=start,
            )
            result = set_agent_heartbeat(
                agent["id"],
                10,
                now=now,
            )
            shown = show_agent(agent["id"])
            self.assertFalse(result["rescheduled"])
            self.assertEqual(shown["state"]["wake_requested_at"], format_utc(start))
            self.assertEqual(shown["state"]["next_wake_at"], format_utc(start))

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
            self.assertIn("exec /tmp/venv/bin/python -m codexapi tick", text)

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

    def test_cron_status_reports_broken_wrapper_python(self):
        with _temp_home() as home:
            write_tick_wrapper(
                home=home,
                python_executable="/tmp/venv/bin/python",
                path_value="/tmp/venv/bin:/usr/bin",
                hostname="host-a",
            )
            crontab = render_cron_line(home=home, hostname="host-a") + "\n"
            with patch("codexapi.agents._read_crontab", return_value=crontab):
                with patch(
                    "codexapi.agents.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["/tmp/venv/bin/python", "-c", "import codexapi"],
                        1,
                        stdout="",
                        stderr="ModuleNotFoundError: No module named 'codexapi'\n",
                    ),
                ):
                    status = cron_status(home=home, hostname="host-a")
                    self.assertTrue(status["configured"])
                    self.assertFalse(status["healthy"])
                    self.assertIn("cannot import codexapi", status["reason"])
                    self.assertFalse(cron_installed(home=home, hostname="host-a"))

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

    def test_nudge_agent_can_spawn_async_process(self):
        calls = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                calls.append((cmd, kwargs))

        with _temp_home() as home:
            agent = start_agent(
                "Handle messages.",
                hostname="host-a",
            )
            with patch("codexapi.agents.subprocess.Popen", FakePopen):
                result = nudge_agent(
                    agent["id"],
                    home=home,
                    hostname="host-a",
                    wait=False,
                )
        self.assertTrue(result["ran"])
        self.assertTrue(result["spawned"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][-2:], ["run", agent["id"]])

    def test_tick_spawns_one_process_per_due_local_agent(self):
        calls = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                calls.append((cmd, kwargs))

        with _temp_home() as home:
            first = start_agent("First job.", hostname="host-a")
            second = start_agent("Second job.", hostname="host-a")
            start_agent("Remote job.", hostname="host-b")
            with patch("codexapi.agents.subprocess.Popen", FakePopen):
                result = tick(home=home, hostname="host-a")
        self.assertTrue(result["ran"])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["woken"], 2)
        self.assertEqual(len(calls), 2)
        spawned = sorted(call[0][-1] for call in calls)
        self.assertEqual(spawned, sorted([first["id"], second["id"]]))

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

    def test_start_agent_replays_start_time_env_on_later_wakes(self):
        captured = {}

        with _temp_home():
            with patch.dict(
                os.environ,
                {
                    "CUSTOM_AGENT_ENV": "expected-value",
                    "GH_TOKEN": "ghp-test-token",
                },
                clear=False,
            ):
                agent = start_agent(
                    "Use my saved environment.",
                    hostname="host-a",
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
                    captured["env"] = dict(env or {})
                    self.thread_id = thread_id
                    self.last_usage = {}

                def __call__(self, prompt):
                    return json.dumps(
                        {
                            "status": "Handled with saved env",
                            "continue": False,
                            "reply": "done",
                        }
                    )

            with patch.dict(
                os.environ,
                {
                    "CUSTOM_AGENT_ENV": "different-value",
                    "GH_TOKEN": "",
                },
                clear=False,
            ):
                with patch("codexapi.agents.Agent", FakeAgent):
                    nudge_agent(agent["id"], hostname="host-a")

            self.assertEqual(captured["env"]["CUSTOM_AGENT_ENV"], "expected-value")
            self.assertEqual(captured["env"]["GH_TOKEN"], "ghp-test-token")

    def test_start_agent_captures_gh_token_when_env_is_missing(self):
        with _temp_home():
            with patch.dict(
                os.environ,
                {"GH_TOKEN": "", "GITHUB_TOKEN": ""},
                clear=False,
            ):
                with patch("codexapi.agents.shutil.which", return_value="/usr/bin/gh"):
                    with patch(
                        "codexapi.agents.subprocess.run",
                        return_value=subprocess.CompletedProcess(
                            ["gh", "auth", "token"],
                            0,
                            stdout="gho-from-gh-auth\n",
                            stderr="",
                        ),
                    ):
                        agent = start_agent(
                            "Use GitHub from cron.",
                            hostname="host-a",
                        )
            shown = show_agent(agent["id"])
            self.assertEqual(shown["session"]["env"]["GH_TOKEN"], "gho-from-gh-auth")

    def test_start_agent_keeps_existing_gh_token_without_calling_gh(self):
        with _temp_home():
            with patch.dict(os.environ, {"GH_TOKEN": "existing-gh-token"}, clear=False):
                with patch("codexapi.agents.shutil.which", return_value="/usr/bin/gh"):
                    with patch("codexapi.agents.subprocess.run") as run_mock:
                        agent = start_agent(
                            "Use existing GitHub token.",
                            hostname="host-a",
                        )
            shown = show_agent(agent["id"])
            self.assertEqual(shown["session"]["env"]["GH_TOKEN"], "existing-gh-token")
            run_mock.assert_not_called()

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
            self.assertIn("QMSG", list_out.getvalue())
            self.assertIn("QCMD", list_out.getvalue())
            self.assertIn("REPO", list_out.getvalue())
            self.assertIn("done", list_out.getvalue())
            self.assertIn("codexapi", list_out.getvalue())

            show_out = io.StringIO()
            with redirect_stdout(show_out):
                _print_managed_agent_show(shown)
            text = show_out.getvalue()
            self.assertIn("Policy: until_done", text)
            self.assertIn("Qmsg: 0  Qcmd: 0", text)
            self.assertIn("Tokens: 50 total (30 in, 20 out, 50.0/h)", text)
            self.assertIn("Prompt: Handle messages.", text)
            self.assertIn("Recent runs:", text)
            self.assertIn("msgs=1", text)

    def test_cli_start_warns_when_cron_missing(self):
        with _temp_home() as home:
            output = io.StringIO()
            errors = io.StringIO()
            with patch("codexapi.agents._ensure_backend_available", return_value="/usr/bin/codex"):
                with patch(
                    "codexapi.cli.agent_cron_status",
                    return_value={"configured": False, "healthy": False, "reason": ""},
                ):
                    with redirect_stdout(output), redirect_stderr(errors):
                        cli_main(["agent", "start", "Handle messages."])
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["waited"])
            warning = errors.getvalue()
            self.assertIn("Background agent wakes will not run", warning)
            self.assertIn(str(home), warning)
            self.assertIn("codexapi agent install-cron", warning)

    def test_cli_start_warns_when_scheduler_is_broken(self):
        output = io.StringIO()
        errors = io.StringIO()
        with _temp_home():
            with patch("codexapi.agents._ensure_backend_available", return_value="/usr/bin/codex"):
                with patch(
                    "codexapi.cli.agent_cron_status",
                    return_value={
                        "configured": True,
                        "healthy": False,
                        "reason": "Wrapper python '/tmp/venv/bin/python' cannot import codexapi.",
                    },
                ):
                    with redirect_stdout(output), redirect_stderr(errors):
                        cli_main(["agent", "start", "Handle messages."])
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["waited"])
        warning = errors.getvalue()
        self.assertIn("installed but not runnable", warning)
        self.assertIn("cannot import codexapi", warning)
        self.assertIn("Reinstall it with", warning)

    def test_cli_start_fails_fast_when_backend_is_missing(self):
        output = io.StringIO()
        errors = io.StringIO()
        with _temp_home():
            with patch(
                "codexapi.agents._ensure_backend_available",
                side_effect=RuntimeError("Codex CLI not found: 'codex'."),
            ):
                with redirect_stdout(output), redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as exc:
                        cli_main(["agent", "start", "Handle messages."])
        self.assertEqual(str(exc.exception), "Codex CLI not found: 'codex'.")

    def test_cli_send_queues_by_default(self):
        with _temp_home():
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "host-a"}, clear=False):
                agent = start_agent(
                    "Handle messages.",
                    hostname="host-a",
                )
                output = io.StringIO()
                with patch(
                    "codexapi.cli.nudge_agent",
                    return_value={"ran": True, "woken": 1, "spawned": True},
                ) as nudge_mock:
                    with redirect_stdout(output):
                        cli_main(["agent", "send", agent["id"], "status"])
                payload = json.loads(output.getvalue())
                self.assertFalse(payload["waited"])
                self.assertTrue(payload["nudge"]["spawned"])
                nudge_mock.assert_called_once_with(agent["id"], wait=False)
                shown = show_agent(agent["id"])
                self.assertEqual(shown["unread_message_count"], 1)
                self.assertEqual(shown["state"]["status"], "ready")

    def test_cli_resume_without_wait_async_nudges_and_list_shows_resuming(self):
        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Still running",
                        "continue": True,
                        "reply": "Continuing.",
                    }
                ),
                "thread_id": "thread-resume-ui",
            }

        with _temp_home():
            agent = start_agent("Keep an eye on this.", hostname="host-a")
            control_agent(agent["id"], "pause", hostname="host-a")
            tick(hostname="host-a", runner=fake_runner)

            output = io.StringIO()
            with patch(
                "codexapi.cli.nudge_agent",
                return_value={"ran": True, "woken": 1, "spawned": True},
            ) as nudge_mock:
                with redirect_stdout(output):
                    cli_main(["agent", "resume", agent["id"]])
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["waited"])
            self.assertTrue(payload["nudge"]["spawned"])
            nudge_mock.assert_called_once_with(agent["id"], wait=False)

            shown = show_agent(agent["id"])
            self.assertEqual(shown["state"]["status"], "paused")
            self.assertEqual(shown["display_status"], "resuming")
            self.assertEqual(shown["pending_commands"], ["resume"])
            self.assertEqual(shown["pending_command_count"], 1)

            list_out = io.StringIO()
            with redirect_stdout(list_out):
                _print_managed_agent_list([shown])
            self.assertIn("resuming", list_out.getvalue())
            self.assertIn("QMSG", list_out.getvalue())
            self.assertIn("QCMD", list_out.getvalue())

            show_out = io.StringIO()
            with redirect_stdout(show_out):
                _print_managed_agent_show(shown)
            text = show_out.getvalue()
            self.assertIn("[resuming]", text)
            self.assertIn("State: paused", text)
            self.assertIn("Pending commands: resume", text)

    def test_cli_send_wait_shows_immediate_agent_reply(self):
        with _temp_home():
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "host-a"}, clear=False):
                agent = start_agent(
                    "Handle messages.",
                    hostname="host-a",
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

                    def __call__(self, prompt):
                        self.thread_id = "thread-cli-send"
                        return json.dumps(
                            {
                                "status": "Answered immediately",
                                "continue": False,
                                "reply": "I saw your note.",
                            }
                        )

                output = io.StringIO()
                with patch("codexapi.agents.Agent", FakeAgent):
                    with redirect_stdout(output):
                        cli_main(["agent", "send", "--wait", agent["id"], "status"])
                payload = json.loads(output.getvalue())
                self.assertTrue(payload["waited"])
                self.assertTrue(payload["nudge"]["woken"])
                self.assertTrue(payload["delivered"])
                self.assertEqual(payload["agent_status"], "Answered immediately")
                self.assertEqual(payload["agent_reply"], "I saw your note.")

    def test_cli_set_heartbeat_updates_agent(self):
        start = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
        later = start + timedelta(minutes=3)

        with _temp_home():
            agent = start_agent(
                "Handle messages.",
                hostname="host-a",
                heartbeat_minutes=30,
                now=start,
            )
            output = io.StringIO()
            with patch("codexapi.agents.utc_now", return_value=later):
                with redirect_stdout(output):
                    cli_main(["agent", "set-heartbeat", agent["id"], "12"])
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["heartbeat_minutes"], 12)
            shown = show_agent(agent["id"])
            self.assertEqual(shown["meta"]["heartbeat_minutes"], 12)

    def test_cli_views_mark_stale_running_agent(self):
        start = datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)
        stale_now = start + timedelta(hours=2)

        with _temp_home() as home:
            agent = start_agent(
                "Handle messages.",
                hostname="host-a",
                heartbeat_minutes=5,
                now=start,
            )
            state_path = home / "agents" / agent["id"] / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "running"
            state["last_wake_at"] = format_utc(start)
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            rollout = home / "rollouts" / "rollout-thread-stale.jsonl"
            _write_rollout(
                rollout,
                [
                    {
                        "timestamp": "2026-03-09T14:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-stale"},
                    },
                    {
                        "timestamp": "2026-03-09T14:00:10Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Still checking.",
                        },
                    },
                ],
            )
            _set_rollout_session(home, agent["id"], "host-a", "thread-stale", rollout)

            lock_path = home / "agents" / agent["id"] / "hosts" / "host-a" / "run.lock"
            with patch("codexapi.agents.utc_now", return_value=stale_now):
                with _try_lock(lock_path) as handle:
                    self.assertIsNotNone(handle)
                    shown = show_agent(agent["id"])
                    status = status_agent(agent["id"])

                    list_out = io.StringIO()
                    with redirect_stdout(list_out):
                        _print_managed_agent_list([shown])

                    show_out = io.StringIO()
                    with redirect_stdout(show_out):
                        _print_managed_agent_show(shown)

                    status_out = io.StringIO()
                    with redirect_stdout(status_out):
                        _print_managed_agent_status(status)

            self.assertTrue(shown["run_lock_held"])
            self.assertTrue(shown["stale"])
            self.assertEqual(shown["last_event_at"], "2026-03-09T14:00:10Z")
            self.assertEqual(status["turn_state"], "stale")
            self.assertEqual(status["last_event_at"], "2026-03-09T14:00:10Z")
            self.assertIn("stale", list_out.getvalue())
            self.assertIn("Stale: yes", show_out.getvalue())
            self.assertIn("Turn: turn-stale [stale]", status_out.getvalue())

    def test_recover_agent_marks_running_agent_error_and_requests_wake(self):
        start = datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)
        recover_at = start + timedelta(hours=2)

        with _temp_home() as home:
            agent = start_agent(
                "Handle messages.",
                hostname="host-a",
                heartbeat_minutes=5,
                now=start,
            )
            state_path = home / "agents" / agent["id"] / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "running"
            state["last_wake_at"] = format_utc(start)
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            lock_path = home / "agents" / agent["id"] / "hosts" / "host-a" / "run.lock"
            lock_path.write_text(
                json.dumps(
                    {"pid": 4321, "hostname": "host-a", "started_at": format_utc(start)}
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "codexapi.agents._agent_runtime",
                return_value={
                    "run_lock_held": True,
                    "last_event_at": "2026-03-09T14:00:10Z",
                    "stale": True,
                    "stale_after_seconds": 1800,
                    "stale_for_seconds": 7190,
                },
            ):
                with patch(
                    "codexapi.agents._recover_run_lock",
                    return_value={
                        "pid": 4321,
                        "pgid": 4321,
                        "sent_sigterm": True,
                        "sent_sigkill": False,
                    },
                ) as recover_lock:
                    result = recover_agent(agent["id"], hostname="host-a", now=recover_at)

            shown = show_agent(agent["id"])
            recover_lock.assert_called_once()
            self.assertEqual(recover_lock.call_args[0][0], lock_path.resolve())
            self.assertTrue(result["recovered"])
            self.assertTrue(result["stale"])
            self.assertEqual(result["signal"]["pid"], 4321)
            self.assertEqual(shown["state"]["status"], "error")
            self.assertEqual(shown["state"]["last_error"], "Recovered stuck wake.")
            self.assertEqual(shown["state"]["wake_requested_at"], format_utc(recover_at))

    def test_cli_recover_wait_nudges_after_recovery(self):
        with _temp_home():
            agent = start_agent("Handle messages.", hostname="host-a")
            output = io.StringIO()
            with patch(
                "codexapi.cli.recover_managed_agent",
                return_value={"id": agent["id"], "name": agent["name"], "status": "error"},
            ) as recover_mock:
                with patch(
                    "codexapi.cli.nudge_agent",
                    return_value={"ran": True, "woken": 1},
                ) as nudge_mock:
                    with redirect_stdout(output):
                        cli_main(["agent", "recover", "--wait", agent["id"]])
            payload = json.loads(output.getvalue())
            recover_mock.assert_called_once_with(agent["id"])
            nudge_mock.assert_called_once_with(agent["id"], wait=True)
            self.assertTrue(payload["waited"])
            self.assertEqual(payload["nudge"]["woken"], 1)

    def test_status_agent_returns_latest_completed_turn(self):
        with _temp_home() as home:
            agent = start_agent("Handle messages.", hostname="host-a")
            rollout = home / "rollouts" / "rollout-thread-status.jsonl"
            _write_rollout(
                rollout,
                [
                    {
                        "timestamp": "2026-03-09T13:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-old"},
                    },
                    {
                        "timestamp": "2026-03-09T13:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Old turn.",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-old",
                            "last_agent_message": "{\"status\":\"Old\",\"continue\":false}",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-new"},
                    },
                    {
                        "timestamp": "2026-03-09T13:10:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Checking the repository state.",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "git status --short"}),
                            "call_id": "call-cmd",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-cmd",
                            "output": "Chunk ID: 123456\nWall time: 0.0100 seconds\nProcess exited with code 0\nOriginal token count: 5\nOutput:\nM README.md\n",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:04Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Updating the agent notes now.",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:05Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "apply_patch",
                            "status": "completed",
                            "call_id": "call-patch",
                            "input": "*** Begin Patch\n*** Update File: /tmp/AGENTBOOK.md\n@@\n-old\n+new\n*** End Patch\n",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:06Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call-patch",
                            "output": json.dumps(
                                {
                                    "output": "Success. Updated the following files:\nM /tmp/AGENTBOOK.md\n",
                                    "metadata": {"exit_code": 0, "duration_seconds": 0.1},
                                }
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:07Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "{\"status\":\"Ready to merge\",\"continue\":false,\"reply\":\"Looks good.\"}",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T13:10:08Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-new",
                            "last_agent_message": "{\"status\":\"Ready to merge\",\"continue\":false,\"reply\":\"Looks good.\"}",
                        },
                    },
                ],
            )
            _set_rollout_session(home, agent["id"], "host-a", "thread-status", rollout)

            result = status_agent(agent["id"], include_actions=True)
            self.assertEqual(result["turn_id"], "turn-new")
            self.assertEqual(result["turn_state"], "complete")
            self.assertEqual(result["started_at"], "2026-03-09T13:10:00Z")
            self.assertEqual(result["ended_at"], "2026-03-09T13:10:08Z")
            self.assertEqual(
                result["progress"],
                [
                    "Checking the repository state.",
                    "Updating the agent notes now.",
                ],
            )
            self.assertEqual(result["final_json"]["status"], "Ready to merge")
            self.assertEqual(result["final_json"]["reply"], "Looks good.")
            self.assertEqual(len(result["tools"]), 2)
            self.assertEqual(result["tools"][0]["name"], "exec_command")
            self.assertEqual(result["tools"][0]["command"], "git status --short")
            self.assertEqual(result["tools"][0]["exit_code"], 0)
            self.assertEqual(result["tools"][0]["output"], "M README.md")
            self.assertEqual(result["tools"][1]["name"], "apply_patch")
            self.assertEqual(result["tools"][1]["files"], ["/tmp/AGENTBOOK.md"])

    def test_status_agent_returns_missing_when_no_rollout_is_known(self):
        with _temp_home():
            agent = start_agent("Handle messages.", hostname="host-a")
            result = status_agent(agent["id"])
            self.assertEqual(result["turn_state"], "missing")
            self.assertEqual(result["rollout_path"], "")
            self.assertEqual(result["progress"], [])

    def test_status_agent_returns_active_turn_when_run_lock_is_held(self):
        with _temp_home() as home:
            agent = start_agent("Handle messages.", hostname="host-a")
            rollout = home / "rollouts" / "rollout-thread-active.jsonl"
            _write_rollout(
                rollout,
                [
                    {
                        "timestamp": "2026-03-09T14:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-active"},
                    },
                    {
                        "timestamp": "2026-03-09T14:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Checking the latest CI run now.",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T14:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "gh pr checks 123"}),
                            "call_id": "call-live",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T14:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-live",
                            "output": "Chunk ID: 654321\nWall time: 0.0100 seconds\nProcess exited with code 0\nOriginal token count: 5\nOutput:\nci / in_progress\n",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T14:00:04Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "The required checks are still running.",
                        },
                    },
                ],
            )
            _set_rollout_session(home, agent["id"], "host-a", "thread-active", rollout)

            lock_path = home / "agents" / agent["id"] / "hosts" / "host-a" / "run.lock"
            with _try_lock(lock_path) as handle:
                self.assertIsNotNone(handle)
                with patch(
                    "codexapi.agents.utc_now",
                    return_value=datetime(2026, 3, 9, 14, 5, tzinfo=timezone.utc),
                ):
                    result = status_agent(agent["id"])

            self.assertEqual(result["turn_id"], "turn-active")
            self.assertEqual(result["turn_state"], "active")
            self.assertEqual(result["ended_at"], "")
            self.assertEqual(result["final_output"], "The required checks are still running.")
            self.assertIsNone(result["final_json"])
            self.assertEqual(
                result["progress"],
                [
                    "Checking the latest CI run now.",
                    "The required checks are still running.",
                ],
            )

    def test_cli_status_shows_latest_turn_details(self):
        with _temp_home() as home:
            agent = start_agent("Handle messages.", hostname="host-a")
            rollout = home / "rollouts" / "rollout-thread-cli.jsonl"
            _write_rollout(
                rollout,
                [
                    {
                        "timestamp": "2026-03-09T15:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-cli"},
                    },
                    {
                        "timestamp": "2026-03-09T15:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Inspecting the latest rollout details.",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T15:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "git status --short"}),
                            "call_id": "call-cli",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T15:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-cli",
                            "output": "Chunk ID: 111111\nWall time: 0.0100 seconds\nProcess exited with code 0\nOriginal token count: 5\nOutput:\nM README.md\n",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T15:00:04Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "{\"status\":\"Handled\",\"continue\":false}",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T15:00:05Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-cli",
                            "last_agent_message": "{\"status\":\"Handled\",\"continue\":false}",
                        },
                    },
                ],
            )
            _set_rollout_session(home, agent["id"], "host-a", "thread-cli", rollout)

            output = io.StringIO()
            with redirect_stdout(output):
                cli_main(["agent", "status", agent["id"]])
            text = output.getvalue()
            self.assertIn("Turn: turn-cli [complete]", text)
            self.assertIn("Progress:", text)
            self.assertIn("Inspecting the latest rollout details.", text)
            self.assertIn("Final fields:", text)
            self.assertIn("Status: Handled", text)
            self.assertNotIn("Final output:", text)
            self.assertNotIn("Actions:", text)

    def test_cli_status_with_actions_shows_tool_summaries(self):
        with _temp_home() as home:
            agent = start_agent("Handle messages.", hostname="host-a")
            rollout = home / "rollouts" / "rollout-thread-cli-actions.jsonl"
            _write_rollout(
                rollout,
                [
                    {
                        "timestamp": "2026-03-09T16:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-cli-actions"},
                    },
                    {
                        "timestamp": "2026-03-09T16:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Inspecting the latest rollout details.",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T16:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "git status --short"}),
                            "call_id": "call-cli-actions",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T16:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-cli-actions",
                            "output": "Chunk ID: 222222\nWall time: 0.0100 seconds\nProcess exited with code 0\nOriginal token count: 5\nOutput:\nM README.md\n",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T16:00:04Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "{\"status\":\"Handled\",\"continue\":false}",
                        },
                    },
                    {
                        "timestamp": "2026-03-09T16:00:05Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-cli-actions",
                            "last_agent_message": "{\"status\":\"Handled\",\"continue\":false}",
                        },
                    },
                ],
            )
            _set_rollout_session(home, agent["id"], "host-a", "thread-cli-actions", rollout)

            output = io.StringIO()
            with redirect_stdout(output):
                cli_main(["agent", "status", "--actions", agent["id"]])
            text = output.getvalue()
            self.assertIn("Actions:", text)
            self.assertIn("Running command: git status --short (exit 0)", text)


if __name__ == "__main__":
    unittest.main()
