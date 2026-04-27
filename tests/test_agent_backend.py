import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.agent import _codex_fast_config, _parse_jsonl, build_agent_flags
from codexapi.async_agent import _build_codex_command, _build_cursor_command


class AgentBackendTests(unittest.TestCase):
    def test_codex_fast_config_defaults_to_normal_mode(self):
        self.assertEqual(_codex_fast_config(False), ["-c", "features.fast_mode=false"])

    def test_codex_fast_config_enables_fast_mode(self):
        self.assertEqual(
            _codex_fast_config(True),
            [
                "-c",
                "service_tier=fast",
                "-c",
                "features.fast_mode=true",
            ],
        )

    def test_async_codex_command_uses_normal_mode_by_default(self):
        command = _build_codex_command(None, True, None)
        self.assertIn("features.fast_mode=false", command)
        self.assertNotIn("service_tier=fast", command)

    def test_async_codex_command_can_enable_fast_mode(self):
        command = _build_codex_command(None, True, None, fast=True)
        self.assertIn("service_tier=fast", command)
        self.assertIn("features.fast_mode=true", command)

    def test_build_agent_flags_maps_codex_model_and_thinking_to_config(self):
        self.assertEqual(
            build_agent_flags(backend="codex", model="gpt-5.5", thinking="xhigh"),
            "-c model=gpt-5.5 -c model_reasoning_effort=xhigh",
        )

    def test_build_agent_flags_maps_cursor_model_to_model_flag(self):
        self.assertEqual(
            build_agent_flags(backend="cursor", model="claude-4"),
            "--model claude-4",
        )

    def test_build_agent_flags_rejects_cursor_thinking(self):
        with self.assertRaises(ValueError):
            build_agent_flags(backend="cursor", thinking="high")

    def test_async_codex_command_can_set_model_and_thinking(self):
        command = _build_codex_command(None, True, None, model="gpt-5.5", thinking="high")
        self.assertIn("model=gpt-5.5", command)
        self.assertIn("model_reasoning_effort=high", command)

    def test_async_cursor_command_can_use_direct_cursor_agent(self):
        with patch("codexapi.async_agent._cursor_command_prefix", return_value=["/tmp/cursor-agent"]):
            command = _build_cursor_command("/tmp/work", True, None, model="composer-2")
        self.assertEqual(command[0], "/tmp/cursor-agent")
        self.assertNotEqual(command[1], "agent")
        self.assertIn("--model", command)
        self.assertIn("composer-2", command)

    def test_parse_jsonl_extracts_last_token_usage(self):
        output = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 7,
                                    "total_tokens": 19,
                                }
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "hello"},
                    }
                ),
            ]
        )
        message, thread_id, usage = _parse_jsonl(output, include_thinking=False)
        self.assertEqual(message, "hello")
        self.assertEqual(thread_id, "thread-1")
        self.assertEqual(
            usage,
            {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
        )

    def test_parse_jsonl_falls_back_to_total_token_usage(self):
        output = "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "prompt_tokens": 9,
                                    "completion_tokens": 4,
                                    "total_tokens": 13,
                                }
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"},
                    }
                ),
            ]
        )
        message, thread_id, usage = _parse_jsonl(output, include_thinking=False)
        self.assertEqual(message, "done")
        self.assertIsNone(thread_id)
        self.assertEqual(
            usage,
            {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
        )


if __name__ == "__main__":
    unittest.main()
