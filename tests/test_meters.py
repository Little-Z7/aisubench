from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aisubench.meters.api_agent import parse_openai_usage
from aisubench.meters.base import Usage
from aisubench.meters.claude import ClaudeMeter, parse_claude_jsonl
from aisubench.meters.kimi import KimiMeter, parse_wire_jsonl
from aisubench.meters.mock import MockMeter


def kimi_record(input_other=10, output=5, cache_read=100, cache_creation=2,
                time_ms=1789111388997, **extra) -> str:
    record = {
        "type": "usage.record",
        "agentId": "main",
        "model": "kimi-for-coding/k3",
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "usageScope": "turn",
        "time": time_ms,
    }
    record.update(extra)
    return json.dumps(record) + "\n"


def claude_record(message_id="msg_1", input_tokens=10, output_tokens=5,
                  cache_read=100, cache_creation=3,
                  timestamp="2026-08-23T17:21:55.767Z") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {
            "id": message_id,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
        "timestamp": timestamp,
    }) + "\n"


class KimiFormatTests(unittest.TestCase):
    def test_real_wire_record_and_millisecond_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_text(kimi_record(input_other=4226, output=359,
                                        cache_read=28672, cache_creation=10))
            usage = parse_wire_jsonl(path)
            self.assertEqual(usage.input, 4236)          # inputOther + inputCacheCreation
            self.assertEqual(usage.cached, 28672)
            self.assertEqual(usage.output, 359)
            self.assertEqual(usage.total, 4236 + 28672 + 359)
            self.assertEqual(usage.requests, 1)
            self.assertAlmostEqual(usage.first_token_ts, 1789111388.997, places=6)

    def test_legacy_flat_usage_still_parses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_text(json.dumps({"usage": {"input": 2, "output": 3}}) + "\n")
            self.assertEqual(parse_wire_jsonl(path).total, 5)

    def test_bad_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_bytes(
                kimi_record().encode()
                + b"\xff\xfe not valid utf-8\n"
                + b"{broken json\n"
                + b"\n"
                + kimi_record(input_other=1, output=1, cache_read=0, cache_creation=0).encode()
            )
            usage = parse_wire_jsonl(path)
            self.assertEqual(usage.requests, 2)
            self.assertEqual(usage.input, 12 + 1)
            self.assertEqual(usage.output, 5 + 1)

    def test_consecutive_collect_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_text(kimi_record(input_other=1, output=1, cache_read=0, cache_creation=0))
            meter = KimiMeter(path)
            first = meter.collect()
            self.assertEqual(first.requests, 1)
            self.assertEqual(meter.collect().requests, 0)
            path.write_text(path.read_text()
                            + kimi_record(input_other=2, output=3, cache_read=0, cache_creation=0))
            second = meter.collect()
            self.assertEqual(second.requests, 1)
            self.assertEqual(second.total, 5)
            self.assertEqual(meter.collect().total, 0)

    def test_truncation_resets_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_text(
                kimi_record(input_other=1, output=1, cache_read=0, cache_creation=0)
                + kimi_record(input_other=2, output=2, cache_read=0, cache_creation=0)
            )
            meter = KimiMeter(path)
            self.assertEqual(meter.collect().total, 6)
            path.write_text(kimi_record(input_other=7, output=8, cache_read=0, cache_creation=0))
            truncated = meter.collect()
            self.assertEqual(truncated.total, 15)
            self.assertEqual(meter.collect().total, 0)


class ClaudeFormatTests(unittest.TestCase):
    def test_duplicate_message_ids_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text(
                claude_record("msg_a")
                + claude_record("msg_a")
                + claude_record("msg_a")
                + claude_record("msg_b", input_tokens=20, output_tokens=7,
                                cache_read=50, cache_creation=0)
            )
            usage = parse_claude_jsonl(path)
            self.assertEqual(usage.requests, 2)
            self.assertEqual(usage.input, 13 + 20)   # cache creation 计入 input
            self.assertEqual(usage.cached, 150)
            self.assertEqual(usage.output, 12)
            self.assertEqual(usage.total, 33 + 150 + 12)

    def test_dedup_persists_across_collects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text(claude_record("msg_a"))
            meter = ClaudeMeter(path)
            self.assertEqual(meter.collect().requests, 1)
            path.write_text(path.read_text()
                            + claude_record("msg_a")
                            + claude_record("msg_b"))
            second = meter.collect()
            self.assertEqual(second.requests, 1)
            self.assertEqual(second.output, 5)
            self.assertEqual(meter.collect().requests, 0)

    def test_truncation_resets_offset_and_dedup_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text(claude_record("msg_a") + claude_record("msg_b"))
            meter = ClaudeMeter(path)
            self.assertEqual(meter.collect().requests, 2)
            path.write_text(claude_record("msg_a", output_tokens=9))
            truncated = meter.collect()
            self.assertEqual(truncated.requests, 1)
            self.assertEqual(truncated.output, 9)

    def test_timestamp_and_iso_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text(claude_record())
            usage = parse_claude_jsonl(path)
            self.assertIsNotNone(usage.first_token_ts)
            self.assertGreater(usage.first_token_ts, 1_700_000_000)


class OpenAiUsageTests(unittest.TestCase):
    def test_nested_cached_tokens(self):
        usage = parse_openai_usage({
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 64},
        })
        self.assertEqual(usage.input, 36)     # prompt_tokens - cached
        self.assertEqual(usage.cached, 64)
        self.assertEqual(usage.output, 20)
        self.assertEqual(usage.total, 120)

    def test_unconvertible_key_falls_through(self):
        usage = parse_openai_usage({"prompt_tokens": "n/a", "input_tokens": 7,
                                    "completion_tokens": 3})
        self.assertEqual(usage.input, 7)
        self.assertEqual(usage.output, 3)


class BaseUsageTests(unittest.TestCase):
    def test_total_includes_cached(self):
        self.assertEqual(Usage(input=1, cached=2, output=3).total, 6)

    def test_mock_meter_is_deterministic(self):
        meter = MockMeter(Usage(input=4, output=5))
        meter.start()
        self.assertEqual(meter.collect().total, 9)
        self.assertEqual(meter.collect().total, 9)


if __name__ == "__main__":
    unittest.main()
