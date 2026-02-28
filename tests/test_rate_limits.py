import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexapi.rate_limits import _extract_rate_limits


def _write_events(path, rates):
    with open(path, "w", encoding="utf-8") as handle:
        for rate in rates:
            event = {"payload": {"rate_limits": rate}}
            handle.write(json.dumps(event))
            handle.write("\n")


class RateLimitsTests(unittest.TestCase):
    def test_extract_prefers_codex_limit_id_when_spark_is_last(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = handle.name
        try:
            _write_events(
                path,
                [
                    {"limit_id": "codex", "primary": {"used_percent": 3}},
                    {
                        "limit_id": "codex_bengalfox",
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "primary": {"used_percent": 0},
                    },
                ],
            )
            found = _extract_rate_limits(path)
        finally:
            os.unlink(path)
        self.assertEqual(found["limit_id"], "codex")
        self.assertEqual(found["primary"]["used_percent"], 3)

    def test_extract_falls_back_to_last_when_codex_missing(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = handle.name
        try:
            _write_events(
                path,
                [
                    {"limit_id": "codex_bengalfox", "primary": {"used_percent": 1}},
                    {"limit_id": "codex_lynx", "primary": {"used_percent": 2}},
                ],
            )
            found = _extract_rate_limits(path)
        finally:
            os.unlink(path)
        self.assertEqual(found["limit_id"], "codex_lynx")

    def test_extract_keeps_legacy_single_limit_without_limit_id(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = handle.name
        try:
            _write_events(path, [{"primary": {"used_percent": 22}}])
            found = _extract_rate_limits(path)
        finally:
            os.unlink(path)
        self.assertEqual(found["primary"]["used_percent"], 22)


if __name__ == "__main__":
    unittest.main()
