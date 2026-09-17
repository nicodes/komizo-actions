"""Read-only notice controls; SSH is mocked or replaced by owned local processes."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "storage_notice", ROOT / "deploy/storage-notice.py"
)
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)


def df(total, available, used=None):
    if used is None:
        used = total - available
    return (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/vda2 {total} {used} {available} 50% /\n"
    ).encode()


class NoticeTests(unittest.TestCase):
    def run_notice(self, payload=None, reason=None):
        out = io.StringIO()
        with patch.object(
            notice, "sample", return_value=(payload, reason)
        ), contextlib.redirect_stdout(out):
            rc = notice.main()
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_healthy_numeric_observation_is_not_admission(self):
        out = self.run_notice(df(30 * 1024**2, 10 * 1024**2))
        self.assertNotIn("::warning::", out)
        self.assertIn("ROOTFS=/", out)
        self.assertRegex(out, r"observed_at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        self.assertIn("available_bytes=10737418240 total_bytes=32212254720", out)
        self.assertIn("free_percent=33.33%", out)
        self.assertIn("not deployment admission", out)

    def test_absolute_reserve_equality_and_one_kib_below(self):
        total = 20 * 1024**2
        at_floor = 5 * 1024**2
        self.assertNotIn("::warning::", self.run_notice(df(total, at_floor)))
        self.assertIn("::warning::", self.run_notice(df(total, at_floor - 1)))

    def test_percentage_reserve_equality_and_below(self):
        total = 100 * 1024**2
        available = total // 5
        self.assertNotIn("::warning::", self.run_notice(df(total, available)))
        self.assertIn("::warning::", self.run_notice(df(total, available - 1)))

    def test_percentage_margin_rounds_up_using_integer_bytes(self):
        total = 100 * 1024**2 + 1
        available = total // 5
        out = self.run_notice(df(total, available))
        self.assertIn("::warning::", out)
        self.assertIn(f"warning_threshold_bytes={(total * 1024 + 4) // 5}", out)
        self.assertNotIn("::warning::", self.run_notice(df(total, available + 1)))

    def test_full_and_small_filesystems_warn(self):
        for total, available in ((30 * 1024**2, 0), (1024, 1024)):
            self.assertIn("::warning::", self.run_notice(df(total, available)))

    def test_unavailable_permission_and_timeout_nonblocking(self):
        for reason in ("lookup_failed", "timeout", "output_limit"):
            out = self.run_notice(reason=reason)
            self.assertIn(f"unavailable ({reason})", out)
            self.assertIn("deployment continues", out)
            self.assertNotIn("available_bytes=", out)

    def test_unknown_reason_and_local_errors_never_echo_values(self):
        self.assertNotIn("SECRET_SENTINEL", self.run_notice(reason="SECRET_SENTINEL"))
        out = io.StringIO()
        with patch.object(
            notice, "sample", side_effect=OSError("SECRET_SENTINEL")
        ), contextlib.redirect_stdout(out):
            self.assertEqual(notice.main(), 0)
        self.assertIn("unavailable (local_probe_failure)", out.getvalue())
        self.assertNotIn("SECRET_SENTINEL", out.getvalue())

    def test_malformed_unknown_and_injected_output_is_not_forwarded(self):
        valid = df(10000000, 6000000)
        for payload in (
            b"",
            b"Permission denied SECRET_SENTINEL",
            b"banner\n" + valid,
            valid + b"::error::SECRET_SENTINEL\n",
            valid.replace(b"6000000", b"-1"),
            valid.replace(b"6000000", b"NaN"),
            valid.replace(b"50% /", b"50% /other"),
            b"\xff",
            b"x" * (notice.MAX_OUTPUT_BYTES + 1),
        ):
            with self.subTest(payload=payload[:20]):
                out = self.run_notice(payload)
                self.assertIn("unavailable (malformed_output)", out)
                self.assertNotIn("SECRET_SENTINEL", out)
                self.assertNotIn("::error::", out)

    def test_inconsistent_and_excessive_counters_deny(self):
        for payload in (
            df(0, 0),
            df(100, 101),
            df(100, 80, 30),
            df(10**16, 1),
            df(100, 1).replace(b"50%", b"101%"),
        ):
            with self.assertRaises(ValueError):
                notice.parse_df(payload)

    def test_reserved_blocks_and_busybox_spacing(self):
        payload = b"Filesystem           1024-blocks    Used Available Capacity Mounted on\n/dev/vda2             10000000 1000000 8000000  12% /\n"
        self.assertEqual(notice.parse_df(payload), (10240000000, 8192000000))

    def test_restored_observation_after_warning(self):
        self.assertIn("::warning::", self.run_notice(df(30 * 1024**2, 1)))
        self.assertNotIn("::warning::", self.run_notice(df(30 * 1024**2, 10 * 1024**2)))


class TransportTests(unittest.TestCase):
    def run_child(self, source, timeout=2):
        with patch.object(
            notice, "SSH", [sys.executable, "-B", "-c", source]
        ), patch.object(notice, "TIMEOUT_SECONDS", timeout):
            return notice.sample()

    def test_ssh_255_discards_both_streams(self):
        out, reason = self.run_child(
            "import sys;print('SECRET_SENTINEL');sys.stderr.write('Permission denied SECRET_SENTINEL');sys.exit(255)"
        )
        self.assertEqual((out, reason), (None, "lookup_failed"))

    def test_stdout_and_stderr_share_output_limit(self):
        for stream in ("stdout", "stderr"):
            out, reason = self.run_child(f"import sys;sys.{stream}.write('x'*9000)")
            self.assertEqual((out, reason), (None, "output_limit"))

    def test_actual_client_timeout_is_bounded_and_reaped(self):
        started = time.monotonic()
        out, reason = self.run_child("import time;time.sleep(10)", timeout=0.05)
        self.assertEqual((out, reason), (None, "timeout"))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_healthy_transport(self):
        expected = df(10000000, 6000000)
        self.assertEqual(
            self.run_child(f"import sys;sys.stdout.buffer.write({expected!r})"),
            (expected, None),
        )

    def test_fixed_readonly_command_and_existing_alias_identity(self):
        self.assertEqual(notice.SSH[-2:], ["deploy-target", "LC_ALL=C df -Pk /"])
        self.assertIn("StrictHostKeyChecking=yes", notice.SSH)
        self.assertIn("UpdateHostKeys=no", notice.SSH)
        self.assertIn("ControlMaster=no", notice.SSH)
        self.assertNotIn("-i", notice.SSH)
        self.assertNotIn("-F", notice.SSH)
        self.assertNotIn("-O", notice.SSH)
        self.assertNotIn("ControlPath=none", notice.SSH)
        self.assertNotIn("sudo", notice.REMOTE_COMMAND)
        self.assertNotIn("doas", notice.REMOTE_COMMAND)
        self.assertNotIn("docker", notice.REMOTE_COMMAND)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.action = yaml.safe_load((ROOT / "deploy/action.yml").read_text())
        self.steps = self.action["runs"]["steps"]
        self.index = next(
            i
            for i, s in enumerate(self.steps)
            if s.get("name") == "Storage notice (warning only)"
        )

    def test_stage_order_and_existing_connection_without_host_input(self):
        connect = next(
            i for i, s in enumerate(self.steps) if "/connect@" in s.get("uses", "")
        )
        validation = next(
            i for i, s in enumerate(self.steps) if s.get("name") == "Validate inputs"
        )
        self.assertLess(validation, connect)
        self.assertLess(connect, self.index)
        for fragment in ("publish-config", "set-secrets", "activate", "health-check"):
            index = next(
                i
                for i, s in enumerate(self.steps)
                if f"/{fragment}@" in s.get("uses", "")
            )
            self.assertGreater(index, self.index)
        self.assertNotIn("if", self.steps[self.index])

    def test_only_notice_is_nonblocking_and_core_pins_unchanged(self):
        self.assertIs(self.steps[self.index]["continue-on-error"], True)
        for i, step in enumerate(self.steps):
            if i != self.index:
                self.assertNotIn("continue-on-error", step)
            if step.get("uses", "").startswith("nicodes/komizo-actions/"):
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")

    def test_local_probe_failure_is_nonblocking_but_core_failure_propagates(self):
        step = self.steps[self.index]["run"]
        # Only override the advisory's python3 function; no external SSH.
        prefix = "python3() { return 77; }; export GITHUB_ACTION_PATH=/unused; "
        result = subprocess.run(
            ["bash", "-ec", prefix + step], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning::Storage notice unavailable", result.stdout)
        for core in ("before", "after"):
            command = prefix + (
                "exit 23;\n" + step if core == "before" else step + "\nexit 23"
            )
            result = subprocess.run(
                ["bash", "-ec", command], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 23)
            if core == "before":
                self.assertEqual(result.stdout, "")

    def test_shared_ci_discovers_notice_tests(self):
        ci = yaml.safe_load((ROOT / ".github/actions/test/action.yml").read_text())
        self.assertTrue(
            any(
                "test_storage_notice.py" in s.get("run", "")
                for s in ci["runs"]["steps"]
            )
        )


if __name__ == "__main__":
    unittest.main()
