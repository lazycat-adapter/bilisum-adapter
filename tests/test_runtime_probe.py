"""Exercise BiliSum's real probe and on-disk cache with a slow subprocess stand-in."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from video_sum_service import runtime_support as runtime


class RuntimeProbeTests(unittest.TestCase):
    def test_cold_start_recovers_from_an_old_failed_probe_and_caches_success(self):
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "environment-probe-cache.json"
            cache.write_text(json.dumps({"base": {"runtimeChannel": "base", "runtimeReady": False}}))
            runtime._environment_probe_cache.clear()
            calls = []

            def slow_probe(command, timeout):
                calls.append(timeout)
                if timeout < 180:
                    raise subprocess.TimeoutExpired(command[:1], timeout)
                return subprocess.CompletedProcess(command, 0, stdout='{"pythonVersion":"3.11"}')

            with patch.object(runtime, "_environment_probe_cache_path", return_value=cache), \
                 patch.object(runtime, "run_host_command", side_effect=slow_probe):
                self.assertTrue(runtime.detect_environment("base")["runtimeReady"])
                runtime._environment_probe_cache.clear()
                self.assertTrue(runtime.detect_environment("base")["runtimeReady"])
                self.assertEqual(len(calls), 1)

    def test_timeout_is_not_written_as_a_successful_probe(self):
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "environment-probe-cache.json"
            runtime._environment_probe_cache.clear()
            with patch.object(runtime, "_environment_probe_cache_path", return_value=cache), \
                 patch.object(runtime, "run_host_command", side_effect=subprocess.TimeoutExpired(["python"], 600)):
                self.assertFalse(runtime.detect_environment("base")["runtimeReady"])
                self.assertNotIn("base", runtime._read_environment_probe_cache_file())
