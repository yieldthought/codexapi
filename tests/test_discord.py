import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.agents import nudge_agent, read_agent, start_agent
from codexapi.cli import main as cli_main
from codexapi.discord import (
    deliver_discord_updates,
    discord_status,
    flush_discord_turns,
    ingest_discord,
    read_discord_bridge,
    setup_discord,
    sync_discord_bridges,
)


@contextmanager
def _temp_home():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"CODEXAPI_HOME": tmpdir, "USER": "tester"}, clear=False):
            yield Path(tmpdir)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _discord_config(home, owner="host-a"):
    return {
        "enabled": True,
        "bot_token": "token-1",
        "bot_user_id": "bot-1",
        "bot_username": "codexapi-bot",
        "application_id": "app-1",
        "guild_id": "guild-1",
        "guild_name": "Codexapi",
        "user_id": "user-1",
        "category_id": "",
        "owner_hostname": owner,
    }


def _write_discord_config(home, owner="host-a"):
    _write_json(home / "discord" / "config.json", _discord_config(home, owner))


def _write_bridge(home, agent_id, channel_id="channel-1", channel_name="test-agent"):
    _write_json(
        home / "discord" / "bridges" / f"{agent_id}.json",
        {
            "agent_id": agent_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "last_message_id": "",
            "pending_user_turn": [],
            "last_sent_run_id": "",
            "active": True,
        },
    )


class DiscordTests(unittest.TestCase):
    def test_setup_discord_writes_config_and_status(self):
        calls = []

        def fake_request(config, method, path, body=None, query=None):
            calls.append((method, path, body, query))
            if path == "/users/@me":
                return {"id": "bot-1", "username": "codexapi-bot"}
            if path == "/guilds/guild-1":
                return {"id": "guild-1", "name": "Codexapi"}
            if path == "/oauth2/applications/@me":
                return {"id": "app-1"}
            raise AssertionError(path)

        with _temp_home() as home:
            with patch("codexapi.discord._discord_request", fake_request):
                result = setup_discord("token-1", "guild-1", "user-1", home=home)
                status = discord_status(home)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["guild_id"], "guild-1")
        self.assertEqual(result["user_id"], "user-1")
        self.assertEqual(status["bot_user_id"], "bot-1")
        self.assertEqual(status["guild_name"], "Codexapi")
        self.assertEqual(len(calls), 3)

    def test_setup_discord_backfills_existing_agents(self):
        calls = []

        def fake_request(config, method, path, body=None, query=None):
            calls.append((method, path, body, query))
            if path == "/users/@me":
                return {"id": "bot-1", "username": "codexapi-bot"}
            if path == "/guilds/guild-1":
                return {"id": "guild-1", "name": "Codexapi"}
            if path == "/oauth2/applications/@me":
                return {"id": "app-1"}
            if path == "/guilds/guild-1/channels":
                return {"id": f"channel-{body['name']}"}
            raise AssertionError(path)

        with _temp_home() as home:
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "host-a"}, clear=False):
                first = start_agent("First existing agent.", hostname="host-a", discord=False)
                second = start_agent("Second existing agent.", hostname="host-a", discord=False)
                with patch("codexapi.discord._discord_request", fake_request):
                    result = setup_discord("token-1", "guild-1", "user-1", home=home)
            first_bridge = read_discord_bridge(first["id"], home)
            second_bridge = read_discord_bridge(second["id"], home)
        self.assertEqual(result["backfilled"], 2)
        self.assertEqual(result["deferred"], 0)
        self.assertTrue(first_bridge["channel_id"])
        self.assertTrue(second_bridge["channel_id"])
        self.assertEqual(first_bridge["channel_name"], f"💤-{first['name']}")
        self.assertEqual(second_bridge["channel_name"], f"💤-{second['name']}")
        self.assertEqual(sum(1 for method, path, _, _ in calls if path == "/guilds/guild-1/channels"), 2)

    def test_setup_discord_backfills_placeholders_when_owner_is_remote(self):
        def fake_request(config, method, path, body=None, query=None):
            if path == "/users/@me":
                return {"id": "bot-1", "username": "codexapi-bot"}
            if path == "/guilds/guild-1":
                return {"id": "guild-1", "name": "Codexapi"}
            if path == "/oauth2/applications/@me":
                return {"id": "app-1"}
            raise AssertionError(path)

        with _temp_home() as home:
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "host-a"}, clear=False):
                agent = start_agent("Existing agent.", hostname="host-a", discord=False)
                with patch("codexapi.discord._discord_request", fake_request):
                    result = setup_discord(
                        "token-1",
                        "guild-1",
                        "user-1",
                        owner_hostname="host-owner",
                        home=home,
                    )
            bridge = read_discord_bridge(agent["id"], home)
        self.assertEqual(result["backfilled"], 0)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(bridge["channel_id"], "")
        self.assertEqual(bridge["channel_name"], f"💤-{agent['name']}")

    def test_start_agent_auto_creates_discord_bridge_when_enabled(self):
        calls = []

        def fake_request(config, method, path, body=None, query=None):
            calls.append((method, path, body, query))
            if path == "/guilds/guild-1/channels":
                return {"id": "channel-auto"}
            raise AssertionError(path)

        with _temp_home() as home:
            _write_discord_config(home)
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "host-a"}, clear=False):
                with patch("codexapi.discord._discord_request", fake_request):
                    agent = start_agent("Handle messages.", hostname="host-a")
            bridge = read_discord_bridge(agent["id"], home)
        self.assertIsNotNone(bridge)
        self.assertEqual(bridge["channel_id"], "channel-auto")
        self.assertEqual(bridge["channel_name"], f"💤-{agent['name']}")
        self.assertTrue(any(body and body.get("name") == f"💤-{agent['name']}" for _, _, body, _ in calls))

    def test_start_agent_on_non_owner_host_defers_channel_creation(self):
        calls = []

        def fake_request(config, method, path, body=None, query=None):
            calls.append((method, path, body, query))
            if path == "/guilds/guild-1/channels":
                return {"id": "channel-later"}
            raise AssertionError(path)

        with _temp_home() as home:
            _write_discord_config(home, owner="host-owner")
            with patch.dict(os.environ, {"CODEXAPI_HOSTNAME": "host-worker"}, clear=False):
                agent = start_agent("Handle messages.", hostname="host-worker")
            deferred = read_discord_bridge(agent["id"], home)
            self.assertEqual(deferred["channel_id"], "")
            with patch("codexapi.discord._discord_request", fake_request):
                result = sync_discord_bridges(home=home, hostname="host-owner")
            bridge = read_discord_bridge(agent["id"], home)
        self.assertEqual(result["created"], 1)
        self.assertEqual(bridge["channel_id"], "channel-later")
        self.assertTrue(any(path == "/guilds/guild-1/channels" for _, path, _, _ in calls))

    def test_cli_agent_start_no_discord_skips_bridge_creation(self):
        output = io.StringIO()
        errors = io.StringIO()

        with _temp_home() as home:
            _write_discord_config(home)
            with patch("codexapi.discord._discord_request") as request_mock:
                with patch("codexapi.cli.agent_cron_installed", return_value=True):
                    with redirect_stdout(output), redirect_stderr(errors):
                        cli_main(["agent", "start", "--no-discord", "Handle messages."])
            payload = json.loads(output.getvalue())
            bridge = read_discord_bridge(payload["id"], home)
        self.assertIsNone(bridge)
        self.assertFalse(request_mock.called)

    def test_discord_ingest_and_flush_batches_messages_into_one_turn(self):
        def fake_request(config, method, path, body=None, query=None):
            if path == "/channels/channel-1/messages" and method == "GET":
                return [
                    {
                        "id": "101",
                        "channel_id": "channel-1",
                        "content": "hey do this",
                        "timestamp": "2026-03-09T10:00:00.000000+00:00",
                        "author": {"id": "user-1", "username": "Mark"},
                    },
                    {
                        "id": "102",
                        "channel_id": "channel-1",
                        "content": "actually do this other thing instead",
                        "timestamp": "2026-03-09T10:00:05.000000+00:00",
                        "author": {"id": "user-1", "username": "Mark"},
                    },
                ]
            if path == "/channels/channel-1/messages" and method == "POST":
                return {"id": "outbound-1"}
            raise AssertionError((method, path))

        nudges = []

        def fake_nudge(agent_ref, home=None, hostname=None, now=None, runner=None, wait=True):
            nudges.append(agent_ref)
            return {"ran": True, "reason": "local", "processed": 1, "woken": 1}

        with _temp_home() as home:
            _write_discord_config(home)
            agent = start_agent("Handle messages.", hostname="host-a", discord=False)
            _write_bridge(home, agent["id"])
            with patch("codexapi.discord._discord_request", fake_request):
                ingest = ingest_discord(home=home, hostname="host-a")
            with patch("codexapi.agents.nudge_agent", fake_nudge):
                flushed = flush_discord_turns(home=home, hostname="host-a")
            conversation = read_agent(agent["id"], home=home)
            bridge = read_discord_bridge(agent["id"], home)
        self.assertEqual(ingest["buffered"], 2)
        self.assertEqual(flushed["flushed"], 1)
        self.assertEqual(flushed["nudged"], 1)
        self.assertEqual(nudges, [agent["id"]])
        self.assertEqual(len(conversation["items"]), 1)
        self.assertEqual(conversation["items"][0]["kind"], "queued")
        self.assertIn("hey do this", conversation["items"][0]["text"])
        self.assertIn("actually do this other thing instead", conversation["items"][0]["text"])
        self.assertEqual(bridge["last_message_id"], "102")

    def test_deliver_discord_updates_sends_run_update(self):
        sent = []

        def fake_request(config, method, path, body=None, query=None):
            sent.append((method, path, body, query))
            if path == "/channels/channel-1/messages":
                return {"id": "msg-1"}
            raise AssertionError((method, path))

        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Handled",
                        "continue": False,
                        "reply": "done",
                        "update": "I handled the task and do not plan more work.",
                    }
                ),
                "thread_id": "thread-discord",
            }

        with _temp_home() as home:
            _write_discord_config(home)
            agent = start_agent("Handle messages.", hostname="host-a", discord=False)
            _write_bridge(home, agent["id"])
            nudge_agent(agent["id"], home=home, hostname="host-a", runner=fake_runner)
            with patch("codexapi.discord._discord_request", fake_request):
                result = deliver_discord_updates(home=home, hostname="host-a")
            bridge = read_discord_bridge(agent["id"], home)
        self.assertEqual(result["sent"], 1)
        self.assertTrue(bridge["last_sent_run_id"])
        self.assertEqual(sent[0][1], "/channels/channel-1/messages")
        self.assertEqual(sent[0][2]["content"], "I handled the task and do not plan more work.")

    def test_local_run_renames_channel_for_running_and_done(self):
        calls = []

        def fake_request(config, method, path, body=None, query=None):
            calls.append((method, path, body, query))
            if path == "/channels/channel-1" and method == "PATCH":
                return {"id": "channel-1"}
            raise AssertionError((method, path))

        def fake_runner(meta, session, prompt):
            return {
                "message": json.dumps(
                    {
                        "status": "Handled",
                        "continue": False,
                        "reply": "done",
                        "update": "I handled the task and do not plan more work.",
                    }
                ),
                "thread_id": "thread-discord",
            }

        with _temp_home() as home:
            _write_discord_config(home)
            agent = start_agent("Handle messages.", hostname="host-a", discord=False)
            _write_bridge(home, agent["id"], channel_name=f"💤-{agent['name']}")
            with patch("codexapi.discord._discord_request", fake_request):
                nudge_agent(agent["id"], home=home, hostname="host-a", runner=fake_runner)
            bridge = read_discord_bridge(agent["id"], home)
        rename_calls = [call for call in calls if call[1] == "/channels/channel-1" and call[0] == "PATCH"]
        self.assertEqual(len(rename_calls), 2)
        self.assertEqual(rename_calls[0][2]["name"], f"🤖-{agent['name']}")
        self.assertEqual(rename_calls[1][2]["name"], f"✅-{agent['name']}")
        self.assertEqual(bridge["channel_name"], f"✅-{agent['name']}")

    def test_sync_discord_bridges_renames_channel_for_remote_status_change(self):
        calls = []

        def fake_request(config, method, path, body=None, query=None):
            calls.append((method, path, body, query))
            if path == "/channels/channel-1" and method == "PATCH":
                return {"id": "channel-1"}
            raise AssertionError((method, path))

        with _temp_home() as home:
            _write_discord_config(home)
            agent = start_agent("Handle messages.", hostname="host-worker", discord=False)
            _write_bridge(home, agent["id"], channel_name=f"💤-{agent['name']}")
            state_path = home / "agents" / agent["id"] / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "paused"
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with patch("codexapi.discord._discord_request", fake_request):
                result = sync_discord_bridges(home=home, hostname="host-a")
            bridge = read_discord_bridge(agent["id"], home)
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(calls[0][2]["name"], f"✋-{agent['name']}")
        self.assertEqual(bridge["channel_name"], f"✋-{agent['name']}")
