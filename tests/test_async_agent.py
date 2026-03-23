import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi import AsyncAgent


class AsyncAgentTests(unittest.TestCase):
    def test_async_agent_reports_rollout_progress_and_final_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            code_home = root / "codex-home"
            workdir = root / "work"
            workdir.mkdir()
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    cwd = ""
                    args = sys.argv[1:]
                    for index, value in enumerate(args):
                        if value == "--cd" and index + 1 < len(args):
                            cwd = args[index + 1]

                    prompt = sys.stdin.read()
                    if not prompt:
                        raise SystemExit("missing prompt")

                    thread_id = "thread-async"
                    print(json.dumps({"type": "thread.started", "thread_id": thread_id}), flush=True)
                    print(json.dumps({"type": "turn.started"}), flush=True)

                    rollout = (
                        Path(os.environ["CODEX_HOME"])
                        / "sessions"
                        / "2026"
                        / "03"
                        / "21"
                        / "rollout-2026-03-21T11-00-00-thread-async.jsonl"
                    )
                    rollout.parent.mkdir(parents=True, exist_ok=True)
                    with open(rollout, "w", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-03-21T11:00:00Z",
                                    "type": "session_meta",
                                    "payload": {
                                        "id": thread_id,
                                        "timestamp": "2026-03-21T11:00:00Z",
                                        "cwd": cwd,
                                        "source": "exec",
                                    },
                                }
                            )
                            + "\\n"
                        )
                        handle.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-03-21T11:00:01Z",
                                    "type": "event_msg",
                                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                                }
                            )
                            + "\\n"
                        )
                        handle.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-03-21T11:00:02Z",
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "agent_message",
                                        "phase": "commentary",
                                        "message": "Inspecting the decode path now.",
                                    },
                                }
                            )
                            + "\\n"
                        )
                        handle.flush()

                    time.sleep(0.05)
                    print(
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "id": "item-1",
                                    "type": "agent_message",
                                    "text": "Wrote AUTODEBUG.md",
                                },
                            }
                        ),
                        flush=True,
                    )
                    print(
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 4,
                                    "total_tokens": 14,
                                },
                            }
                        ),
                        flush=True,
                    )
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

            with patch.dict(
                os.environ,
                {"CODEX_HOME": str(code_home), "USER": "tester"},
                clear=False,
            ):
                with patch("codexapi.async_agent._CODEX_BIN", str(fake_codex)):
                    agent = AsyncAgent.start(
                        "Investigate the bug.",
                        cwd=str(workdir),
                        backend="codex",
                        name="async-test",
                    )
                    updates = list(agent.watch(poll_interval=0.01))
                    final = agent.status()

            self.assertGreaterEqual(len(updates), 1)
            self.assertEqual(final["status"], "done")
            self.assertEqual(final["thread_id"], "thread-async")
            self.assertIn("Inspecting the decode path now.", final["progress"])
            self.assertEqual(final["final_output"], "Wrote AUTODEBUG.md")
            self.assertEqual(
                agent.last_usage,
                {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            )


if __name__ == "__main__":
    unittest.main()
